"""The core loop: one version through every stage (TDD 3).

`run_version` is the only place that sequences the stages and touches the
database. Every stage is a pure function over records; this module supplies them
with inputs, writes an event for each result, and decides what not to do:
a rejected citation skips mapping, a non-material change stops after
verification, and a newly created duty skips the mapping call entirely.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db, diff, events, extract, graph, ingest, mapping, routing, verify
from .models import Event

ACTOR = "system"
DB_PATH = "strata.db"
VERSIONS = ("v1", "v2", "v3")


def _append(conn, event: Event) -> Event:
    """Append without committing. run_version commits once, at the end."""
    return events.append(conn, event, commit=False)


def _event(type: str, subject_id: str, payload: dict, project_id: str) -> Event:
    return Event(
        seq=None,
        ts=datetime.now(timezone.utc).isoformat(),
        actor=ACTOR,
        type=type,
        subject_id=subject_id,
        payload=payload,
        project_id=project_id,
    )


def _prior_version(conn, version_id: str) -> str | None:
    """Return the version before this one, or None if it is the first."""
    row = conn.execute(
        "select version_id from versions where number = (select number - 1 from"
        " versions where version_id=?)",
        (version_id,),
    ).fetchone()
    return row["version_id"] if row else None


def _paragraphs(conn) -> dict:
    return {r["para_id"]: r["text"] for r in conn.execute("select para_id, text from paragraphs")}


def _overrides_for(state, change, obligations) -> list:
    """Return links from the human override table, or [] if none apply.

    Checked before the mapping model is asked, so a correction Dana made for an
    earlier version is applied rather than re-proposed (PRD R4.4).
    """
    links = []
    for obligation in obligations:
        correction = events.override_for(state, change, obligation.node_id)
        if correction is None:
            continue
        links.append(
            mapping.Link(
                claim_id="",
                obligation_id=correction.get("obligation_id", obligation.node_id),
                confidence=float(correction.get("confidence", 1.0)),
                rationale=correction.get("rationale", "Human correction carried forward."),
                rationale_quote=correction.get("rationale_quote", ""),
                rationale_status="verified",
            )
        )
    return links


def run_version(conn, project_id: str, version_id: str) -> dict:
    """Run one version through the pipeline, writing an event per result."""
    from_version = _prior_version(conn, version_id)
    summary = {
        "from_version": from_version,
        "to_version": version_id,
        "changes": 0,
        "claims": 0,
        "tasks": [],
    }
    if from_version is None:
        return summary

    changes = diff.diff_versions(conn, from_version, version_id)
    summary["changes"] = len(changes)

    state = events.replay(conn, project_id)
    company = graph.load(conn, events.effective(conn, project_id))
    obligations = [n for n in company.nodes.values() if n.type == "obligation"]
    obligations.sort(key=lambda n: int(n.node_id.split("-")[1]))
    paragraphs = _paragraphs(conn)

    tasks = []
    try:
        tasks = _run_changes(conn, project_id, changes, paragraphs, state, company,
                             obligations, summary)
    except Exception:
        # A version run is all or nothing. A failure part way through would
        # otherwise leave claims for some changes and not others, and a retry
        # would append a second copy of everything that did succeed.
        conn.rollback()
        raise
    conn.commit()
    summary["tasks"] = tasks
    return summary


def _run_changes(conn, project_id, changes, paragraphs, state, company, obligations,
                 summary) -> list:
    """Process every change of one version pair. Writes are not committed here."""
    tasks = []
    for change in changes:
        # Tasks are collected for the whole change, then merged, because one
        # paragraph that alters two obligations produces one claim per
        # obligation and both reach the same downstream nodes.
        for_change = []
        for claim in extract.extract_claims(conn, change):
            summary["claims"] += 1
            for_change.extend(
                _run_claim(conn, project_id, change, claim, paragraphs, state,
                           company, obligations)
            )
        tasks.extend(_emit(conn, project_id, routing.merge_tasks(for_change)))
    return tasks


def _run_claim(conn, project_id, change, claim, paragraphs, state, company,
               obligations) -> list:
    """Verify one claim, map it, propagate, and return its tasks.

    Tasks are returned rather than written: run_version merges a change's tasks
    across its claims before any task_created event is appended.
    """
    _append(conn, _event("claim_extracted", claim.claim_id,
                               claim.__dict__, project_id))

    verification = verify.verify_claim(claim, paragraphs, change)
    _append(conn, _event("claim_verified", claim.claim_id,
                               verification.__dict__, project_id))

    # A rejected quote on a real change is still a change someone must look at,
    # so this is decided before materiality (TDD 3.4).
    if verification.status == "rejected":
        return routing.create_tasks(claim, verification, [], [], company)

    if not claim.material:
        return []

    if claim.obligation_change == "created":
        links = []  # no existing node can be the link for a new duty (TDD 3.6)
    else:
        links = _overrides_for(state, change, obligations)
        if not links:
            links = mapping.propose_links(claim, obligations)
        links = [
            mapping.Link(**{**link.__dict__, "claim_id": claim.claim_id})
            for link in links
        ]
        for link in links:
            _append(conn, _event("link_proposed", claim.claim_id,
                                       link.__dict__, project_id))

    impacts = []
    for link in links:
        for impact in graph.propagate(company, link.obligation_id):
            impacts.append(impact)
            _append(conn, _event(
                "impact_found", claim.claim_id,
                {**impact.__dict__, "claim_id": claim.claim_id}, project_id))

    return routing.create_tasks(claim, verification, links, impacts, company)


def _emit(conn, project_id: str, tasks: list) -> list:
    """Write a task_created event for each task and return them."""
    for task in tasks:
        _append(conn, _event("task_created", task.task_id,
                                   task.__dict__, project_id))
    return tasks


def _load_env(path: str = ".env") -> None:
    """Read KEY=value lines into the environment. Only `make live` needs this."""
    env = Path(path)
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


OBL_12 = {
    "id": "OBL-12",
    "type": "obligation",
    "name": "Penalty for a missed study deadline",
    "text": "Pay a penalty of $500 per business day for each business day a completed"
            " interconnection study is delivered after the required deadline.",
    "owner": "P-2",
    "source_para": "v2:p17",
}
OBL_12_EDGE = {"from": "DOC-1", "to": "OBL-12", "type": "implements"}


def create_obl_12(conn, project_id: str, tasks: list) -> None:
    """Resolve the CH-8 expert item the way the review center would (TDD 3.9).

    The new penalty duty in v2 has no existing node, so it is escalated. An
    expert approves that task and creates the obligation, which makes it a
    mapping candidate for v3, where CH-17 softens the same penalty.
    """
    escalated = [t for t in tasks if t.reason == "new_obligation"]
    for task in escalated:
        events.append(conn, Event(
            seq=None, ts=datetime.now(timezone.utc).isoformat(), actor="expert",
            type="task_approved", subject_id=task.task_id,
            payload={"task_id": task.task_id, "claim_id": task.claim_id},
            project_id=project_id,
        ))
    events.append(conn, Event(
        seq=None, ts=datetime.now(timezone.utc).isoformat(), actor="expert",
        type="node_created", subject_id="OBL-12", payload=OBL_12,
        project_id=project_id,
    ))
    events.append(conn, Event(
        seq=None, ts=datetime.now(timezone.utc).isoformat(), actor="expert",
        type="edge_created", subject_id="DOC-1->OBL-12", payload=OBL_12_EDGE,
        project_id=project_id,
    ))
    print(f"  resolved {len(escalated)} expert item(s); created OBL-12 and one edge")


def _ingest_all(conn) -> None:
    ingest.ingest_company(conn, "data/company/meridian.json")
    for version in VERSIONS:
        ingest.ingest_version(conn, f"data/proceeding/{version}.md")


def main(argv: list[str]) -> int:
    """make reset, make run and make live enter here."""
    command = argv[1] if len(argv) > 1 else "ingest"
    conn = db.connect(DB_PATH)
    db.init_schema(conn)

    if command == "ingest":
        _ingest_all(conn)
        print(f"ingested {len(VERSIONS)} versions and the company graph into {DB_PATH}")
        return 0

    if command == "bootstrap":
        # make run: ingest and process v1 -> v2 from cache, so the review page
        # has something on it the first time it is opened.
        _ingest_all(conn)
        run = run_version(conn, "proj-1", VERSIONS[1])
        print(
            f"{run['from_version']} -> {run['to_version']}: {run['changes']} changes,"
            f" {run['claims']} claims, {len(run['tasks'])} tasks"
        )
        print("start the server and open http://localhost:8000")
        return 0

    if command in ("run", "live"):
        if command == "live":
            _load_env()
            if not os.environ.get("ANTHROPIC_API_KEY"):
                print("make live needs ANTHROPIC_API_KEY; put it in .env")
                return 2
        _ingest_all(conn)
        for version in VERSIONS[1:]:
            run = run_version(conn, "proj-1", version)
            print(
                f"{run['from_version']} -> {run['to_version']}: {run['changes']} changes,"
                f" {run['claims']} claims, {len(run['tasks'])} tasks"
            )
            # Between the two runs an expert resolves the new-obligation item,
            # so v3's mapping call has OBL-12 to link CH-17 to.
            if version == "v2":
                create_obl_12(conn, "proj-1", run["tasks"])
        return 0

    print(f"unknown command {command!r}; expected ingest, bootstrap, run or live")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
