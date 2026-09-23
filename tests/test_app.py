"""Endpoints and templates (PRD R4.5)."""

import pytest
from fastapi.testclient import TestClient

from strata import app as app_module
from strata import db, events, ingest, pipeline

PROJECT = "proj-1"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    path = tmp_path / "strata.db"
    conn = db.connect(path)
    db.init_schema(conn)
    pipeline._ingest_all(conn)
    pipeline.run_version(conn, PROJECT, "v2")
    conn.close()

    monkeypatch.setattr(app_module, "DB_PATH", str(path))
    return TestClient(app_module.app)


def test_root_redirects_to_review(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert "/review" in r.headers["location"]


def test_review_page_renders(client):
    r = client.get(f"/projects/{PROJECT}/review")
    assert r.status_code == 200
    assert "Review center" in r.text


def test_review_shows_a_verification_badge(client):
    body = client.get(f"/projects/{PROJECT}/review").text
    assert "verified" in body


def test_review_shows_a_path_string(client):
    body = client.get(f"/projects/{PROJECT}/review").text
    assert "OBL-3 &rarr; PRJ-1 &rarr; DOC-4" in body or "OBL-3 → PRJ-1 → DOC-4" in body


def test_review_groups_by_change(client):
    body = client.get(f"/projects/{PROJECT}/review").text
    assert "c:v1-&gt;v2:p12" in body or "c:v1->v2:p12" in body
    assert "v1:p12" in body


def test_review_shows_claim_confidence_action_and_assignee(client):
    body = client.get(f"/projects/{PROJECT}/review").text
    assert "confidence" in body.lower()
    assert "Update the document to reflect the changed requirement" in body
    assert "P-2" in body


def test_review_shows_the_quote(client):
    body = client.get(f"/projects/{PROJECT}/review").text
    assert "thirty (30) business days" in body


def test_review_has_action_buttons(client):
    body = client.get(f"/projects/{PROJECT}/review").text
    for action in ("approve", "edit", "escalate"):
        assert f"/{action}" in body


def test_queue_page_shows_reasons(client):
    r = client.get(f"/projects/{PROJECT}/queue")
    assert r.status_code == 200
    assert "new_obligation" in r.text


def test_queue_shows_the_create_obligation_form(client):
    body = client.get(f"/projects/{PROJECT}/queue").text
    assert "create-obligation" in body
    assert 'name="owner"' in body
    assert 'name="source_para"' in body


def test_audit_page_lists_events_newest_first(client):
    r = client.get(f"/projects/{PROJECT}/audit")
    assert r.status_code == 200
    seqs = [int(s) for s in __import__("re").findall(r'data-seq="(\d+)"', r.text)]
    assert seqs == sorted(seqs, reverse=True)
    assert len(seqs) > 10


def test_audit_has_a_rollback_control(client):
    body = client.get(f"/projects/{PROJECT}/audit").text
    assert "rollback" in body
    assert 'name="to_seq"' in body


def test_approve_appends_an_event(client):
    conn = db.connect(app_module.DB_PATH)
    task = events.replay(conn, PROJECT).open_tasks()[0]
    before = len(events.history(conn, PROJECT))
    conn.close()

    r = client.post(f"/tasks/{task['task_id']}/approve", follow_redirects=False)
    assert r.status_code in (302, 303, 307)

    conn = db.connect(app_module.DB_PATH)
    log = events.history(conn, PROJECT)
    assert len(log) == before + 1
    assert log[-1].type == "task_approved"
    assert log[-1].actor == "dana"
    assert events.replay(conn, PROJECT).tasks[task["task_id"]]["status"] == "approved"
    conn.close()


def test_escalate_appends_an_event(client):
    conn = db.connect(app_module.DB_PATH)
    task = events.replay(conn, PROJECT).open_tasks()[0]
    conn.close()
    client.post(f"/tasks/{task['task_id']}/escalate")
    conn = db.connect(app_module.DB_PATH)
    assert events.replay(conn, PROJECT).tasks[task["task_id"]]["status"] == "escalated"
    conn.close()


def test_edit_records_an_override_with_both_keys(client):
    """The edit form is the path that creates an R4.4 override."""
    conn = db.connect(app_module.DB_PATH)
    task = next(t for t in events.replay(conn, PROJECT).open_tasks()
                if t.get("node_id", "").startswith("OBL"))
    conn.close()

    client.post(f"/tasks/{task['task_id']}/edit", data={"obligation_id": "OBL-5"})

    conn = db.connect(app_module.DB_PATH)
    edited = [e for e in events.history(conn, PROJECT) if e.type == "task_edited"]
    assert len(edited) == 1
    payload = edited[0].payload
    assert payload["obligation_id"] == "OBL-5"
    assert payload["change_signature"].startswith("edit:")
    assert payload["lineage_key"].startswith("lineage:")
    assert events.replay(conn, PROJECT).overrides
    conn.close()


def test_create_obligation_appends_node_created(client):
    conn = db.connect(app_module.DB_PATH)
    task = next(t for t in events.replay(conn, PROJECT).open_tasks()
                if t.get("reason") == "new_obligation")
    conn.close()

    r = client.post(
        f"/tasks/{task['task_id']}/create-obligation",
        data={"node_id": "OBL-12", "name": "Penalty", "text": "Pay $500 per day.",
              "owner": "P-2", "source_para": "v2:p17"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303, 307)

    conn = db.connect(app_module.DB_PATH)
    created = [e for e in events.history(conn, PROJECT) if e.type == "node_created"]
    assert len(created) == 1
    assert created[0].payload["id"] == "OBL-12"
    assert created[0].payload["owner"] == "P-2"
    assert created[0].payload["source_para"] == "v2:p17"
    assert created[0].actor == "expert"
    state = events.replay(conn, PROJECT)
    assert state.tasks[task["task_id"]]["status"] == "approved"
    conn.close()


def test_rollback_control_appends_and_replay_honours_it(client):
    conn = db.connect(app_module.DB_PATH)
    task = events.replay(conn, PROJECT).open_tasks()[0]
    conn.close()
    client.post(f"/tasks/{task['task_id']}/approve")

    conn = db.connect(app_module.DB_PATH)
    seq_before_approval = events.replay(conn, PROJECT).last_seq - 1
    conn.close()

    client.post(f"/projects/{PROJECT}/rollback", data={"to_seq": seq_before_approval})

    conn = db.connect(app_module.DB_PATH)
    state = events.replay(conn, PROJECT)
    assert state.rolled_back_to == seq_before_approval
    assert state.tasks[task["task_id"]]["status"] == "open"
    conn.close()

    assert f"rolled back to {seq_before_approval}" in client.get(
        f"/projects/{PROJECT}/audit"
    ).text


def test_state_endpoint_shows_an_earlier_point(client):
    r = client.get(f"/projects/{PROJECT}/state", params={"upto": 5})
    assert r.status_code == 200
    assert "as of event 5" in r.text


def test_load_next_version_runs_the_pipeline(client):
    """The real journey: resolve the new-obligation item, then load v3.

    The cached v3 mapping responses were recorded with OBL-12 in the candidate
    list, and an obligation's name and text are part of that prompt, so the
    expert item has to be resolved with the same values before v3 will run from
    cache. pipeline.OBL_12 is what make live used.
    """
    conn = db.connect(app_module.DB_PATH)
    task = next(t for t in events.replay(conn, PROJECT).open_tasks()
                if t.get("reason") == "new_obligation")
    before = len(events.replay(conn, PROJECT).tasks)
    conn.close()

    client.post(f"/tasks/{task['task_id']}/create-obligation", data={
        "node_id": pipeline.OBL_12["id"], "name": pipeline.OBL_12["name"],
        "text": pipeline.OBL_12["text"], "owner": pipeline.OBL_12["owner"],
        "source_para": pipeline.OBL_12["source_para"],
    })

    r = client.post(f"/projects/{PROJECT}/versions", data={"version_id": "v3"},
                    follow_redirects=False)
    assert r.status_code in (302, 303, 307)

    conn = db.connect(app_module.DB_PATH)
    state = events.replay(conn, PROJECT)
    assert len(state.tasks) > before
    assert any(c["change_id"].startswith("c:v2->v3") for c in state.claims.values())
    conn.close()


def test_review_page_has_the_load_next_version_button(client):
    body = client.get(f"/projects/{PROJECT}/review").text
    assert "Load next version" in body
    assert 'name="version_id"' in body
