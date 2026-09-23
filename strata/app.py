"""FastAPI app: review center, expert queue, audit page (TDD 6).

Every page is rendered from a replay of the event log, so what the screen shows
and what the audit trail says can never disagree. There is no JavaScript beyond
form submission, and no client-side state.
"""

from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import db, diff, events, graph, ingest, llm, pipeline
from .models import COMPANY_ID, Event

COMPANY_FILE = Path("data/company/meridian.json")
DB_PATH = "strata.db"
PROJECT = "proj-1"
VERSIONS = ("v1", "v2", "v3")

app = FastAPI(title="Strata")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


HUMAN_EVENTS = {"task_approved", "task_edited", "task_escalated", "node_created"}
STATUS_WORDS = {"proposed": "proposed", "revised_proposed": "revised proposed",
                "final": "final"}


@dataclass(frozen=True)
class Paragraph:
    """One version pair's worth of orientation (TDD 6.2)."""

    pair: str
    heading: str
    sentences: list[str]
    next_step: str | None = None
    about_link: bool = False


def connect():
    conn = db.connect(DB_PATH)
    db.init_schema(conn)
    return conn


def _changes(conn) -> dict:
    """Index every change of every ingested pair by change_id."""
    found = {}
    numbers = [r["version_id"] for r in conn.execute(
        "select version_id from versions order by number")]
    for a, b in zip(numbers, numbers[1:]):
        for change in diff.diff_versions(conn, a, b):
            found[change.change_id] = change
    return found


def versions_view(conn, state) -> list[dict]:
    """Every ingested version with what has been done to it (TDD 6.1)."""
    processed = {}
    for claim in state.claims.values():
        change_id = claim.get("change_id") or ""
        if "->" not in change_id:
            continue
        to_version = change_id.split(":")[1].split("->")[1]
        processed.setdefault(to_version, set()).add(change_id)

    rows = []
    for row in conn.execute("select * from versions order by number"):
        version_id = row["version_id"]
        changes = len(processed.get(version_id, ()))
        if row["number"] == 1:
            status, loadable = "baseline", False
        elif changes:
            status, loadable = "processed", False
        else:
            status, loadable = "not yet loaded", True
        rows.append({
            "version_id": version_id, "number": row["number"],
            "doc_status": STATUS_WORDS.get(row["status"], row["status"]),
            "issued": row["issued"], "effective": row["effective"],
            "status": status, "changes": changes, "loadable": loadable,
        })
    # Only the earliest unloaded version can be loaded next.
    seen_loadable = False
    for row in rows:
        if row["loadable"] and not seen_loadable:
            seen_loadable = True
        elif row["loadable"]:
            row["loadable"] = False
    return rows


def _pair_facts(state, to_version: str) -> dict:
    """Counts for one version pair, all read from project state."""
    claims = [c for c in state.claims.values()
              if f"->{to_version}:" in (c.get("change_id") or "")]
    changes = {c["change_id"] for c in claims}
    material = {c["change_id"] for c in claims if c.get("material")}
    claim_ids = {c["claim_id"] for c in claims}
    tasks = [t for t in state.tasks.values() if t.get("claim_id") in claim_ids]
    rejected = [v for k, v in state.verifications.items()
                if k in claim_ids and v.get("status") == "rejected"]
    return {
        "changes": len(changes), "material": len(material),
        "owner": len({t["task_id"] for t in tasks if t.get("queue") == "owner"}),
        "expert": len({t["task_id"] for t in tasks if t.get("queue") == "expert"}),
        "rejected": len(rejected),
        "status": _majority_status(claims),
    }


def _majority_status(claims) -> str | None:
    """The version status the claims voted for, or None when there are no votes."""
    votes: dict[str, int] = {}
    for claim in claims:
        status = claim.get("version_status")
        if status:
            votes[status] = votes.get(status, 0) + 1
    return max(votes, key=votes.get) if votes else None


def _next_step(state, versions) -> str:
    """The one thing to do next, by the rules in TDD 6.2."""
    blocking = [t for t in state.tasks.values()
                if t.get("reason") == "new_obligation" and t.get("status") == "open"]
    if blocking:
        return ("Start with the expert queue: a new obligation must be created before"
                " the next version is loaded.")
    unloaded = [v for v in versions if v["status"] == "not yet loaded"]
    if unloaded:
        return (f"The expert queue is clear. Version {unloaded[0]['number']} is ready"
                " to load.")
    return "Every version has been loaded."


def orientation(state, versions) -> list[Paragraph]:
    """One paragraph per processed version pair, newest first (PRD R4.6, TDD 6.2).

    Everything here is computed from project state and the versions table. No
    sentence is written per version, so a fourth version file would produce a
    correct banner with no template change.
    """
    by_id = {v["version_id"]: v for v in versions}
    processed = [v for v in versions if v["status"] == "processed"]
    first_visit = not any(
        t.get("status") in ("approved", "edited", "escalated")
        for t in state.tasks.values()
    ) and not state.created_nodes

    paragraphs = []
    for index, version in enumerate(reversed(processed)):
        facts = _pair_facts(state, version["version_id"])
        previous = by_id.get(f"v{version['number'] - 1}")
        heading = (
            f"Version {version['number']}, {version['doc_status']}, issued"
            f" {version['issued']}, compared against version"
            f" {previous['number'] if previous else version['number'] - 1}."
        )
        sentences = [
            f"{facts['changes']} paragraphs changed, {facts['material']} of them"
            f" material.",
            f"{facts['owner']} task(s) went to owners and {facts['expert']} to the"
            f" expert queue.",
        ]
        if facts["rejected"]:
            sentences.append(
                f"{facts['rejected']} citation(s) did not verify and were held back"
                " for an expert."
            )
        else:
            sentences.append("Every citation verified against the source text.")
        if facts["status"] == "final":
            sentences.append("The rule is now final.")
        paragraphs.append(Paragraph(
            pair=f"v{version['number'] - 1} to {version['version_id']}",
            heading=heading,
            sentences=sentences,
            next_step=_next_step(state, versions) if index == 0 else None,
            about_link=first_visit and index == 0,
        ))
    return paragraphs


def _order(change) -> tuple:
    """Sort key: newest version pair first, then paragraph order within it."""
    if change is None:
        return (0, 0)
    to_number = int(change.to_version.lstrip("v"))
    para = change.para_id_to or change.para_id_from or ":p0"
    return (-to_number, int(para.split(":p")[1]))


def _next_version(conn, state) -> str | None:
    """The next version to load, or None when every version has been run."""
    done = {c["change_id"].split(":")[1].split("->")[1]
            for c in state.claims.values() if c.get("change_id")}
    for version in VERSIONS[1:]:
        if version not in done:
            return version
    return None


def review_items(conn, state, changes) -> list[dict]:
    """Group claims and tasks by change.

    Tasks hang off the change, not the claim: a task can be merged from several
    claims of one change, so a claim does not own it.
    """
    company = graph.load(conn, events.effective(conn, PROJECT))
    grouped: dict[str, dict] = {}
    for claim_id, claim in state.claims.items():
        change_id = claim["change_id"]
        change = changes.get(change_id)
        item = grouped.setdefault(change_id, {
            "change_id": change_id,
            "change": change,
            "kind": change.kind if change else "",
            "from_para": change.para_id_from if change else None,
            "to_para": change.para_id_to if change else None,
            "claims": [],
            "tasks": [],
        })
        item["claims"].append({
            "claim": claim,
            "verification": state.verifications.get(claim_id, {}),
            "links": state.links.get(claim_id, []),
        })

    for task in state.tasks.values():
        change_id = task.get("change_id")
        if change_id not in grouped:
            continue
        node = company.nodes.get(task.get("node_id") or "")
        task["node_type"] = node.type if node else ""
        task["node_name"] = node.name if node else ""
        grouped[change_id]["tasks"].append(task)

    for item in grouped.values():
        item["tasks"].sort(key=lambda t: (t.get("queue", ""), t.get("node_id") or ""))
        change = item["change"]
        item["pair"] = (
            f"{change.from_version} to {change.to_version}" if change else "unknown"
        )
        item["order"] = _order(change)

        item["material"] = any(c["claim"].get("material") for c in item["claims"])
        item["no_task_reason"] = (
            "not material" if not item["material"] else "no downstream nodes"
        )

    # Newest version pair first: what the version you just loaded changed is the
    # reason you are on this page, and appending it below the previous pair put
    # it off the bottom of the screen.
    return sorted(grouped.values(), key=lambda i: i["order"])


def by_pair(items) -> list[dict]:
    """Group review items by version pair, material changes before the rest.

    Non-material changes are collapsed behind one summary line (TDD 6.3), with
    the citation count taken from the hidden changes only.
    """
    pairs: dict[str, dict] = {}
    for item in items:
        pair = pairs.setdefault(item["pair"], {"pair": item["pair"],
                                               "material": [], "quiet": []})
        pair["material" if item["material"] else "quiet"].append(item)

    for pair in pairs.values():
        unverified = sum(
            1
            for item in pair["quiet"]
            for claim in item["claims"]
            if claim["verification"].get("status") != "verified"
        )
        pair["quiet_count"] = len(pair["quiet"])
        pair["quiet_citations"] = (
            "all citations verified" if not unverified
            else f"{unverified} citation(s) not verified"
        )
    return list(pairs.values())


def _render(request, template, **context):
    conn = context.pop("conn")
    state = events.replay(conn, PROJECT)
    return templates.TemplateResponse(
        request, template,
        {"project_id": PROJECT, "state": state, **context},
    )


@app.get("/")
def home():
    return RedirectResponse(f"/projects/{PROJECT}/review", status_code=307)


def company_name() -> str:
    """The company's display name, from the graph file rather than a constant."""
    import json

    try:
        return json.loads(COMPANY_FILE.read_text(encoding="utf-8"))["company_name"]
    except (OSError, KeyError, ValueError):
        return COMPANY_ID


def _layout(conn, project_id: str, state) -> dict:
    """The sidebar and header, rendered from the same state the page uses."""
    version = conn.execute("select docket, title from versions order by number").fetchone()
    versions = versions_view(conn, state)
    return {
        "project_id": project_id,
        "state": state,
        "company_name": company_name(),
        "docket": version["docket"] if version else "",
        "proceeding_title": version["title"] if version else "",
        "versions": versions,
        "open_owner": len([t for t in state.open_tasks() if t.get("queue") == "owner"]),
        "open_expert": len([t for t in state.open_tasks() if t.get("queue") == "expert"]),
    }


def _review_context(conn, project_id: str, error: str | None = None) -> dict:
    state = events.replay(conn, project_id)
    blocking = [
        t for t in state.tasks.values()
        if t.get("reason") == "new_obligation" and t.get("status") == "open"
    ]
    layout = _layout(conn, project_id, state)
    return {
        **layout,
        "items": review_items(conn, state, _changes(conn)),
        "next_version": _next_version(conn, state),
        "owner_count": len([t for t in state.tasks.values() if t.get("queue") == "owner"]),
        "expert_count": len([t for t in state.tasks.values() if t.get("queue") == "expert"]),
        "blocking": blocking,
        "error": error,
        "orientation": orientation(state, layout["versions"]),
        "pairs": by_pair(review_items(conn, state, _changes(conn))),
    }


@app.get("/projects/{project_id}/review")
def review(request: Request, project_id: str):
    conn = connect()
    return templates.TemplateResponse(
        request, "review.html", _review_context(conn, project_id)
    )


@app.get("/projects/{project_id}/queue")
def queue(request: Request, project_id: str):
    conn = connect()
    state = events.replay(conn, project_id)
    company = graph.load(conn, events.effective(conn, project_id))
    items = []
    for task in state.tasks.values():
        if task.get("queue") != "expert":
            continue
        claim = state.claims.get(task.get("claim_id"), {})
        items.append({
            "task": task,
            "claim": claim,
            "verification": state.verifications.get(task.get("claim_id"), {}),
            "source_para": claim.get("quote_para_id", ""),
        })
    people = sorted(
        [n for n in company.nodes.values() if n.type == "person"],
        key=lambda n: n.node_id,
    )
    # Obligations an expert has already created in this project. When one exists,
    # a second new-obligation item should normally be linked to it rather than
    # creating a second node for the same paragraph.
    created = []
    for node in state.created_nodes:
        if node.get("type") == "obligation" and node["id"] not in {c["id"] for c in created}:
            created.append(node)
    return templates.TemplateResponse(request, "queue.html", {
        **_layout(conn, project_id, state),
        "items": items, "people": people, "created": created,
        # The form is prefilled with the obligation make live created, so the
        # default path through the UI produces the same v3 mapping prompts that
        # the committed cache holds (see NOTES, task 15).
        "suggested": pipeline.OBL_12,
    })


@app.get("/projects/{project_id}/audit")
def audit(request: Request, project_id: str, upto: int | None = None):
    conn = connect()
    log = events.history(conn, project_id, upto)
    state = events.replay(conn, project_id, upto)
    return templates.TemplateResponse(request, "audit.html", {
        **_layout(conn, project_id, state),
        "log": list(reversed(log)), "upto": upto,
    })


@app.get("/projects/{project_id}/state")
def state_at(request: Request, project_id: str, upto: int | None = None):
    return audit(request, project_id, upto)


@app.get("/projects/{project_id}/about")
def about(request: Request, project_id: str):
    """Static prose over live values (PRD R4.8, TDD 6.4)."""
    conn = connect()
    state = events.replay(conn, project_id)
    counts = {
        row["type"]: row["n"]
        for row in conn.execute("select type, count(*) as n from nodes group by type")
    }
    return templates.TemplateResponse(request, "about.html", {
        **_layout(conn, project_id, state),
        "node_count": sum(counts.values()),
        "node_counts": counts,
        "edge_count": conn.execute("select count(*) from edges").fetchone()[0],
        "paragraph_count": conn.execute("select count(*) from paragraphs").fetchone()[0],
        "cache_entries": len(list(llm.CACHE_DIR.glob("*.json"))),
        "event_count": len(events.history(conn, project_id)),
    })


@app.get("/projects/{project_id}/versions/{version_id}")
def version_text(request: Request, project_id: str, version_id: str):
    """One version's text as issued, with paragraph IDs and anchors (PRD R4.9).

    Rendered from the versions and paragraphs tables, so what is on the screen is
    what the verifier searched, not a re-read of the file.
    """
    conn = connect()
    row = conn.execute(
        "select * from versions where version_id=? and company_id=?",
        (version_id, COMPANY_ID),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no version {version_id!r}")

    paragraphs = list(conn.execute(
        "select para_id, number, section, text from paragraphs where version_id=?"
        " order by number",
        (version_id,),
    ))
    state = events.replay(conn, project_id)
    return templates.TemplateResponse(request, "version.html", {
        **_layout(conn, project_id, state),
        "version": row,
        "doc_status": STATUS_WORDS.get(row["status"], row["status"]),
        "paragraphs": paragraphs,
    })


@app.post("/projects/{project_id}/versions")
def load_version(request: Request, project_id: str, version_id: str = Form(...)):
    """Run the next version. A cache miss is an expected outcome, not a crash.

    The recorded responses for v3 list the obligation an expert creates while
    resolving v2's new-obligation item, so loading v3 first produces a prompt
    that was never recorded. run_version rolls its own writes back, and the
    review page says what to do.
    """
    conn = connect()
    try:
        pipeline.run_version(conn, project_id, version_id)
    except llm.CacheMiss as miss:
        return templates.TemplateResponse(
            request, "review.html",
            _review_context(conn, project_id, error=str(miss)),
            status_code=409,
        )
    return RedirectResponse(f"/projects/{project_id}/review", status_code=303)


def _append(conn, project_id, type, subject_id, payload, actor="dana"):
    from datetime import datetime, timezone

    events.append(conn, Event(
        seq=None, ts=datetime.now(timezone.utc).isoformat(), actor=actor, type=type,
        subject_id=subject_id, payload=payload, project_id=project_id,
    ))


def _task(conn, task_id):
    """Return the task with this id, or 404. Never invents one.

    Every route below writes an event about the task it was given. Defaulting to
    an empty task on a miss meant an unknown id appended an event anyway, and a
    replay then showed a task that the pipeline never created.
    """
    state = events.replay(conn, PROJECT)
    task = state.tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"no task {task_id!r}")
    return task, state


@app.post("/tasks/{task_id}/approve")
def approve(task_id: str):
    conn = connect()
    task, _ = _task(conn, task_id)
    _append(conn, PROJECT, "task_approved", task_id,
            {"task_id": task_id, "claim_id": task.get("claim_id")})
    return RedirectResponse(f"/projects/{PROJECT}/review", status_code=303)


@app.post("/tasks/{task_id}/escalate")
def escalate(task_id: str):
    conn = connect()
    task, _ = _task(conn, task_id)
    _append(conn, PROJECT, "task_escalated", task_id,
            {"task_id": task_id, "claim_id": task.get("claim_id")})
    return RedirectResponse(f"/projects/{PROJECT}/review", status_code=303)


@app.post("/tasks/{task_id}/edit")
def edit(task_id: str, obligation_id: str = Form(...), note: str = Form("")):
    """Record a correction as an override, keyed so it carries into later versions."""
    conn = connect()
    task, _ = _task(conn, task_id)
    change = _changes(conn).get(task.get("change_id"))
    payload = {
        "task_id": task_id,
        "claim_id": task.get("claim_id"),
        "obligation_id": obligation_id,
        "correction": {"obligation_id": obligation_id, "confidence": 1.0,
                       "rationale": note or "Corrected in review."},
    }
    if change is not None:
        payload["change_signature"] = events.signature(change)
        payload["lineage_key"] = events.lineage_key(change.text_to)
    _append(conn, PROJECT, "task_edited", task_id, payload)
    return RedirectResponse(f"/projects/{PROJECT}/review", status_code=303)


@app.post("/tasks/{task_id}/create-obligation")
def create_obligation(
    task_id: str,
    node_id: str = Form(...),
    name: str = Form(...),
    text: str = Form(""),
    owner: str = Form(...),
    source_para: str = Form(""),
):
    conn = connect()
    task, _ = _task(conn, task_id)
    _append(conn, PROJECT, "node_created", node_id, {
        "id": node_id, "type": "obligation", "name": name, "text": text,
        "owner": owner, "source_para": source_para or None,
    }, actor="expert")
    _append(conn, PROJECT, "task_approved", task_id,
            {"task_id": task_id, "claim_id": task.get("claim_id")}, actor="expert")
    return RedirectResponse(f"/projects/{PROJECT}/queue", status_code=303)


@app.post("/tasks/{task_id}/link-obligation")
def link_obligation(task_id: str, obligation_id: str = Form(...)):
    """Resolve a new-obligation item against an obligation already created.

    The penalty paragraph raises two created claims, and both describe the same
    new duty from different angles. Creating a second node for the second item
    would duplicate the obligation and change the candidate list the next
    version maps against, which the recorded cache does not hold.
    """
    conn = connect()
    task, _ = _task(conn, task_id)
    _append(conn, PROJECT, "task_approved", task_id, {
        "task_id": task_id, "claim_id": task.get("claim_id"),
        "claim_ids": task.get("claim_ids"), "obligation_id": obligation_id,
    }, actor="expert")
    return RedirectResponse(f"/projects/{PROJECT}/queue", status_code=303)


@app.post("/projects/{project_id}/rollback")
def rollback(project_id: str, to_seq: int = Form(...)):
    conn = connect()
    events.rollback(conn, project_id, to_seq)
    return RedirectResponse(f"/projects/{project_id}/audit", status_code=303)
