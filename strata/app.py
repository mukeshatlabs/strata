"""FastAPI app: review center, expert queue, audit page (TDD 6).

Every page is rendered from a replay of the event log, so what the screen shows
and what the audit trail says can never disagree. There is no JavaScript beyond
form submission, and no client-side state.
"""

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import db, diff, events, graph, ingest, pipeline
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


def _next_version(conn, state) -> str | None:
    """The next version to load, or None when every version has been run."""
    done = {c["change_id"].split(":")[1].split("->")[1]
            for c in state.claims.values() if c.get("change_id")}
    for version in VERSIONS[1:]:
        if version not in done:
            return version
    return None


def review_items(conn, state, changes) -> list[dict]:
    """Group the log's claims and tasks by the change they came from."""
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
        })
        tasks = [t for t in state.tasks.values() if t.get("claim_id") == claim_id]
        for task in tasks:
            node = company.nodes.get(task.get("node_id") or "")
            task["node_type"] = node.type if node else ""
            task["node_name"] = node.name if node else ""
        item["claims"].append({
            "claim": claim,
            "verification": state.verifications.get(claim_id, {}),
            "links": state.links.get(claim_id, []),
            "tasks": sorted(tasks, key=lambda t: (t.get("queue", ""), t.get("node_id") or "")),
        })
    return list(grouped.values())


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


@app.get("/projects/{project_id}/review")
def review(request: Request, project_id: str):
    conn = connect()
    state = events.replay(conn, project_id)
    changes = _changes(conn)
    return templates.TemplateResponse(request, "review.html", {
        "project_id": project_id,
        "state": state,
        "items": review_items(conn, state, changes),
        "next_version": _next_version(conn, state),
        "owner_count": len([t for t in state.tasks.values() if t.get("queue") == "owner"]),
        "expert_count": len([t for t in state.tasks.values() if t.get("queue") == "expert"]),
    })


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
    return templates.TemplateResponse(request, "queue.html", {
        "project_id": project_id, "state": state, "items": items, "people": people,
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
def load_version(project_id: str, version_id: str = Form(...)):
    conn = connect()
    pipeline.run_version(conn, project_id, version_id)
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


@app.post("/projects/{project_id}/rollback")
def rollback(project_id: str, to_seq: int = Form(...)):
    conn = connect()
    events.rollback(conn, project_id, to_seq)
    return RedirectResponse(f"/projects/{project_id}/audit", status_code=303)
