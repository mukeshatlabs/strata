"""Append-only event log, replay, and rollback (TDD 3.9).

Project state is never stored. It is computed by folding the log in sequence
order, so "what did the system know at the time" and "undo that" are the same
operation: a replay with a different endpoint. Nothing is ever deleted; a
rollback is itself an event, and it appears in the audit list.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import verify
from .models import COMPANY_ID, Event

HUMAN_ACTIONS = {"task_approved": "approved", "task_edited": "edited",
                 "task_escalated": "escalated"}
ACCEPTING = ("task_approved", "task_edited")


@dataclass
class State:
    """Project state as of some point in the log."""

    claims: dict = field(default_factory=dict)
    verifications: dict = field(default_factory=dict)
    links: dict = field(default_factory=dict)
    impacts: dict = field(default_factory=dict)
    accepted_claims: dict = field(default_factory=dict)
    confirmed_impacts: dict = field(default_factory=dict)
    tasks: dict = field(default_factory=dict)
    overrides: dict = field(default_factory=dict)
    created_nodes: list = field(default_factory=list)
    created_edges: list = field(default_factory=list)
    rolled_back_to: int | None = None
    last_seq: int = 0

    def open_tasks(self) -> list[dict]:
        return [t for t in self.tasks.values() if t["status"] == "open"]

    def closed_tasks(self) -> list[dict]:
        return [t for t in self.tasks.values() if t["status"] != "open"]


def signature(change) -> str:
    """Identify an edit by its content, so it survives renumbering.

    Hashes the kind and both normalized texts, and no paragraph or version ID:
    the same edit keeps its signature wherever the paragraph ends up, and a
    different edit to the same paragraph gets a different one.
    """
    body = "\n\x00\n".join(
        [change.kind, verify.normalize(change.text_from)[0],
         verify.normalize(change.text_to)[0]]
    )
    return "edit:" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def lineage_key(text: str) -> str:
    """Identify a paragraph's content, for following a correction forward.

    An override is stored under the normalized to-text of the change it
    corrected. When a later version edits that same text, the new change's
    from-text hashes to the same key and the correction is found.
    """
    normalized, _ = verify.normalize(text)
    return "lineage:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def append(conn, event: Event) -> Event:
    """Write one event and return it with its assigned sequence number."""
    cursor = conn.execute(
        "insert into events (company_id, project_id, ts, actor, type, subject_id,"
        " payload) values (?,?,?,?,?,?,?)",
        (event.company_id, event.project_id, event.ts or _now(), event.actor,
         event.type, event.subject_id, json.dumps(event.payload, ensure_ascii=False)),
    )
    conn.commit()
    return Event(
        seq=cursor.lastrowid, ts=event.ts, actor=event.actor, type=event.type,
        subject_id=event.subject_id, payload=event.payload,
        project_id=event.project_id, company_id=event.company_id,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def history(conn, project_id: str, upto: int | None = None) -> list[Event]:
    """Every event for a project in sequence order, rollbacks included."""
    sql = "select * from events where project_id=? and company_id=?"
    args = [project_id, COMPANY_ID]
    if upto is not None:
        sql += " and seq<=?"
        args.append(upto)
    return [
        Event(seq=r["seq"], ts=r["ts"], actor=r["actor"], type=r["type"],
              subject_id=r["subject_id"], payload=json.loads(r["payload"]),
              project_id=r["project_id"], company_id=r["company_id"])
        for r in conn.execute(sql + " order by seq", args)
    ]


def effective(conn, project_id: str, upto: int | None = None) -> list[Event]:
    """The events that still count, after applying every rollback in the log.

    A rollback undoes events that precede it, so this is resolved in its own
    pass before anything is folded. The rollback events themselves are dropped
    from the result; `history` keeps them for the audit page.
    """
    kept: list[Event] = []
    for event in history(conn, project_id, upto):
        if event.type == "rollback":
            limit = event.payload.get("to_seq", 0)
            kept = [e for e in kept if e.seq <= limit]
            continue
        kept.append(event)
    return kept


def replay(conn, project_id: str, upto: int | None = None) -> State:
    """Fold the log into project state (PRD R4.2)."""
    state = State()
    for event in history(conn, project_id, upto):
        state.last_seq = event.seq
        if event.type == "rollback":
            state.rolled_back_to = event.payload.get("to_seq", 0)

    for event in effective(conn, project_id, upto):
        _apply(state, event)
    return state


def _apply(state: State, event: Event) -> None:
    """Fold one event into the state."""
    body = event.payload
    if event.type == "claim_extracted":
        state.claims[body["claim_id"]] = body
    elif event.type == "claim_verified":
        state.verifications[body["claim_id"]] = body
    elif event.type == "link_proposed":
        state.links.setdefault(body["claim_id"], []).append(body)
    elif event.type == "impact_found":
        state.impacts.setdefault(body["claim_id"], []).append(body)
    elif event.type == "task_created":
        state.tasks[body["task_id"]] = {**body, "status": "open"}
    elif event.type in HUMAN_ACTIONS:
        task = state.tasks.setdefault(body["task_id"], {"task_id": body["task_id"]})
        task["status"] = HUMAN_ACTIONS[event.type]
        task["actor"] = event.actor
        if event.type == "task_edited":
            _record_override(state, body)
        if event.type in ACCEPTING:
            _accept(state, task)
    elif event.type == "node_created":
        state.created_nodes.append(body)
    elif event.type == "edge_created":
        state.created_edges.append(body)


def _accept(state: State, task: dict) -> None:
    """A human approval is what moves a claim and its impacts into the state."""
    claim_id = task.get("claim_id")
    if not claim_id:
        return
    state.accepted_claims[claim_id] = state.claims.get(claim_id, {"claim_id": claim_id})
    for impact in state.impacts.get(claim_id, []):
        confirmed = state.confirmed_impacts.setdefault(claim_id, [])
        if impact not in confirmed:
            confirmed.append(impact)


def _record_override(state: State, body: dict) -> None:
    """Store a correction under both its edit signature and its lineage key."""
    obligation_id = body.get("obligation_id")
    correction = body.get("correction")
    if obligation_id is None or correction is None:
        return
    for key in (body.get("change_signature"), body.get("lineage_key")):
        if key:
            state.overrides[(key, obligation_id)] = correction


def override_for(state: State, change, obligation_id: str):
    """Return a human correction for this change, or None (PRD R4.4).

    Matches either the edit's own signature, which catches the same edit seen
    again, or the lineage key of the change's from-text, which catches a later
    version editing the text a corrected change produced.
    """
    for key in (signature(change), lineage_key(change.text_from)):
        found = state.overrides.get((key, obligation_id))
        if found is not None:
            return found
    return None


def rollback(conn, project_id: str, to_seq: int, actor: str = "dana") -> Event:
    """Append a rollback event. Nothing is deleted (PRD R4.2)."""
    return append(
        conn,
        Event(seq=None, ts=_now(), actor=actor, type="rollback",
              subject_id=project_id, payload={"to_seq": to_seq},
              project_id=project_id),
    )
