"""FastAPI app: review center, expert queue, audit page (TDD 6).

Every page is rendered from a replay of the event log, so what the screen shows
and what the audit trail says can never disagree. There is no JavaScript beyond
form submission, and no client-side state.
"""

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import db, diff, events, graph, ingest, llm, pipeline
from .models import Event

DB_PATH = "strata.db"
PROJECT = "proj-1"
VERSIONS = ("v1", "v2", "v3")

app = FastAPI(title="Strata")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


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

    # Newest version pair first: what the version you just loaded changed is the
    # reason you are on this page, and appending it below the previous pair put
    # it off the bottom of the screen.
    return sorted(grouped.values(), key=lambda i: i["order"])


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


def _review_context(conn, project_id: str, error: str | None = None) -> dict:
    state = events.replay(conn, project_id)
    blocking = [
        t for t in state.tasks.values()
        if t.get("reason") == "new_obligation" and t.get("status") == "open"
    ]
    return {
        "project_id": project_id,
        "state": state,
        "items": review_items(conn, state, _changes(conn)),
        "next_version": _next_version(conn, state),
        "owner_count": len([t for t in state.tasks.values() if t.get("queue") == "owner"]),
        "expert_count": len([t for t in state.tasks.values() if t.get("queue") == "expert"]),
        "blocking": blocking,
        "error": error,
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
        "project_id": project_id, "state": state, "items": items, "people": people,
        "created": created,
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
        "project_id": project_id, "state": state,
        "log": list(reversed(log)), "upto": upto,
    })


@app.get("/projects/{project_id}/state")
def state_at(request: Request, project_id: str, upto: int | None = None):
    return audit(request, project_id, upto)


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
    state = events.replay(conn, PROJECT)
    return state.tasks.get(task_id, {"task_id": task_id}), state


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
