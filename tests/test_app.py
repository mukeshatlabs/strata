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


def _form_defaults(html_text: str) -> dict:
    """Read the create-obligation form's prefilled values out of the page."""
    import re

    form = html_text[html_text.index("create-obligation"):]
    form = form[: form.index("</form>")]
    text = re.search(r"<textarea[^>]*>(.*?)</textarea>", form, re.S).group(1).strip()
    flat = " ".join(form.split())  # the markup wraps; assertions should not care
    data = dict(re.findall(r'<input name="(\w+)"[^>]*?value="([^"]*)"', flat))
    data["text"] = text
    data["owner"] = re.search(r'<option value="(P-\d+)"[^>]*selected', flat).group(1)
    return data


def test_create_obligation_form_is_prefilled(client):
    data = _form_defaults(client.get(f"/projects/{PROJECT}/queue").text)
    assert data["node_id"] == pipeline.OBL_12["id"]
    assert data["name"] == pipeline.OBL_12["name"]
    assert data["text"] == pipeline.OBL_12["text"]
    assert data["owner"] == pipeline.OBL_12["owner"]
    assert data["source_para"] == pipeline.OBL_12["source_para"]


def test_prefilled_form_then_next_version_runs_without_a_key(client, monkeypatch):
    """Accepting the defaults keeps the whole journey on the committed cache."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    conn = db.connect(app_module.DB_PATH)
    task = next(t for t in events.replay(conn, PROJECT).open_tasks()
                if t.get("reason") == "new_obligation")
    conn.close()

    data = _form_defaults(client.get(f"/projects/{PROJECT}/queue").text)
    posted = client.post(f"/tasks/{task['task_id']}/create-obligation", data=data,
                         follow_redirects=False)
    assert posted.status_code in (302, 303, 307)

    loaded = client.post(f"/projects/{PROJECT}/versions", data={"version_id": "v3"},
                         follow_redirects=False)
    assert loaded.status_code in (302, 303, 307), "v3 must run from cache"

    conn = db.connect(app_module.DB_PATH)
    state = events.replay(conn, PROJECT)
    assert any(c["change_id"].startswith("c:v2->v3") for c in state.claims.values())
    assert "OBL-12" in {n["id"] for n in state.created_nodes}
    conn.close()

    assert client.get(f"/projects/{PROJECT}/review").status_code == 200


def test_loading_the_next_version_too_early_explains_instead_of_crashing(client, monkeypatch):
    """Clicking Load next version before resolving the expert queue (bug report)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    conn = db.connect(app_module.DB_PATH)
    before = len(events.history(conn, PROJECT))
    conn.close()

    r = client.post(f"/projects/{PROJECT}/versions", data={"version_id": "v3"})
    assert r.status_code == 409, "a cache miss is an expected outcome, not a 500"
    assert "not in the recorded cache" in r.text
    assert "expert queue" in r.text
    assert "rolled back" in r.text

    conn = db.connect(app_module.DB_PATH)
    log = events.history(conn, PROJECT)
    assert len(log) == before, "a failed run must leave no events behind"
    assert not [e for e in log if "v2->v3" in (e.subject_id or "")]
    conn.close()


def test_review_warns_while_a_new_obligation_item_is_open(client):
    """One warning, in the banner's next step, not a second copy above it."""
    body = client.get(f"/projects/{PROJECT}/review").text
    assert "a new obligation must be created" in body
    assert "Open the expert queue" in body
    assert "2 item(s) waiting" in body
    assert body.count("Open the expert queue") == 1


def _resolve_new_obligation_items(client) -> int:
    """Resolve every new-obligation item the way the queue offers it.

    The first creates the obligation; the rest link to it, because the penalty
    paragraph raises two claims about one new duty.
    """
    conn = db.connect(app_module.DB_PATH)
    tasks = [t for t in events.replay(conn, PROJECT).open_tasks()
             if t.get("reason") == "new_obligation"]
    conn.close()
    assert tasks, "expected at least one new-obligation item"
    for index, task in enumerate(tasks):
        if index == 0:
            client.post(f"/tasks/{task['task_id']}/create-obligation", data={
                "node_id": pipeline.OBL_12["id"], "name": pipeline.OBL_12["name"],
                "text": pipeline.OBL_12["text"], "owner": pipeline.OBL_12["owner"],
                "source_para": pipeline.OBL_12["source_para"],
            })
        else:
            client.post(f"/tasks/{task['task_id']}/link-obligation",
                        data={"obligation_id": pipeline.OBL_12["id"]})
    return len(tasks)


def test_the_warning_clears_once_every_item_is_resolved(client):
    """The v2 penalty paragraph raises two created claims, so both must be resolved."""
    assert _resolve_new_obligation_items(client) == 2
    body = client.get(f"/projects/{PROJECT}/review").text
    assert "Open the expert queue" not in body
    assert "expert queue is clear" in body, "the next step moves on"


def test_a_failed_run_can_be_retried_after_resolving(client, monkeypatch):
    """The whole point of the rollback: the retry must not double up."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert client.post(f"/projects/{PROJECT}/versions",
                       data={"version_id": "v3"}).status_code == 409

    _resolve_new_obligation_items(client)
    client.post(f"/projects/{PROJECT}/versions", data={"version_id": "v3"},
                follow_redirects=False)

    conn = db.connect(app_module.DB_PATH)
    claims = [c for c in events.replay(conn, PROJECT).claims.values()
              if c["change_id"].startswith("c:v2->v3")]
    ids = [c["claim_id"] for c in claims]
    assert len(ids) == len(set(ids)), "the failed attempt was appended twice"
    conn.close()


def test_queue_offers_create_before_any_obligation_exists(client):
    body = client.get(f"/projects/{PROJECT}/queue").text
    assert "create-obligation" in body
    assert "link-obligation" not in body, "nothing to link to yet"


def test_queue_offers_linking_once_an_obligation_exists(client):
    conn = db.connect(app_module.DB_PATH)
    first = next(t for t in events.replay(conn, PROJECT).open_tasks()
                 if t.get("reason") == "new_obligation")
    conn.close()
    client.post(f"/tasks/{first['task_id']}/create-obligation", data={
        "node_id": pipeline.OBL_12["id"], "name": pipeline.OBL_12["name"],
        "text": pipeline.OBL_12["text"], "owner": pipeline.OBL_12["owner"],
        "source_para": pipeline.OBL_12["source_para"],
    })
    body = client.get(f"/projects/{PROJECT}/queue").text
    assert "link-obligation" in body
    assert "OBL-12 — Penalty for a missed study deadline" in body
    assert "genuinely separate duty" in body, "creating another is still offered"


def test_linking_resolves_without_a_second_node(client):
    resolved = _resolve_new_obligation_items(client)
    assert resolved == 2

    conn = db.connect(app_module.DB_PATH)
    state = events.replay(conn, PROJECT)
    created = [n["id"] for n in state.created_nodes]
    assert created == ["OBL-12"], f"one node, not one per item: {created}"
    assert not [t for t in state.open_tasks() if t.get("reason") == "new_obligation"]

    linked = [e for e in events.history(conn, PROJECT)
              if e.type == "task_approved" and e.payload.get("obligation_id")]
    assert len(linked) == 1
    assert linked[0].payload["obligation_id"] == "OBL-12"
    assert linked[0].actor == "expert"
    conn.close()


def test_link_then_next_version_runs_from_cache(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _resolve_new_obligation_items(client)
    r = client.post(f"/projects/{PROJECT}/versions", data={"version_id": "v3"},
                    follow_redirects=False)
    assert r.status_code == 303, "linking must keep the obligation list the cache assumes"


def test_review_orders_the_newest_version_pair_first(client, monkeypatch):
    """After loading v3, its changes must be at the top, not appended below v2's."""
    import re

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _resolve_new_obligation_items(client)
    assert client.post(f"/projects/{PROJECT}/versions", data={"version_id": "v3"},
                       follow_redirects=False).status_code == 303

    body = client.get(f"/projects/{PROJECT}/review").text
    ids = [i.replace("&gt;", ">") for i in re.findall(r"<h2>(c:[^<]+)</h2>", body)]
    pairs = [i.split(":")[1] for i in ids]
    assert pairs[0] == "v2->v3", f"newest pair must lead, got {pairs[0]}"
    assert pairs[-1] == "v1->v2"
    assert pairs == sorted(pairs, reverse=True), "pairs must not interleave"

    # A removal's id carries a marker, c:v2->v3:-p14, so read the trailing number.
    def para_number(change_id):
        return int(re.search(r"p(\d+)$", change_id).group(1))

    # Within a pair, material changes come first (TDD 6.3), each group in
    # paragraph order, so check the groups rather than the whole run.
    quiet = body[body.index("<details"):]
    quiet_ids = {i.replace("&gt;", ">") for i in re.findall(r"<h2>(c:[^<]+)</h2>", quiet)}
    for pair in ("v2->v3", "v1->v2"):
        group = [i for i in ids if i.startswith(f"c:{pair}") and i not in quiet_ids]
        assert group == sorted(group, key=para_number), f"{pair} material order"


def test_review_labels_each_version_pair(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _resolve_new_obligation_items(client)
    client.post(f"/projects/{PROJECT}/versions", data={"version_id": "v3"})

    body = client.get(f"/projects/{PROJECT}/review").text
    assert "v2 to v3" in body
    assert "v1 to v2" in body
    assert body.index("v2 to v3") < body.index("v1 to v2")


def test_a_single_pair_still_renders_one_heading(client):
    body = client.get(f"/projects/{PROJECT}/review").text
    assert body.count('class="pair"') == 1
    assert "v1 to v2" in body


# --- task 17: orientation, layout, about (PRD R4.6-R4.8, TDD 6.1-6.4) ---


def _load_v3(client):
    _resolve_new_obligation_items(client)
    return client.post(f"/projects/{PROJECT}/versions", data={"version_id": "v3"},
                       follow_redirects=False)


def test_orientation_after_bootstrap_names_the_version_and_counts(client):
    conn = db.connect(app_module.DB_PATH)
    state = events.replay(conn, PROJECT)
    paragraphs = app_module.orientation(state, app_module.versions_view(conn, state))
    conn.close()

    assert len(paragraphs) == 1, "one paragraph per processed pair"
    p = paragraphs[0]
    assert p.pair == "v1 to v2"
    assert "version 2" in p.heading.lower()
    assert "version 1" in p.heading.lower()
    assert "revised" in p.heading.lower()
    assert "2026-05-14" in p.heading

    body = " ".join(p.sentences)
    assert "14" in body, "paragraphs changed"
    assert "material" in body
    assert "expert" in body


def test_orientation_next_step_is_the_expert_queue_while_unresolved(client):
    conn = db.connect(app_module.DB_PATH)
    state = events.replay(conn, PROJECT)
    p = app_module.orientation(state, app_module.versions_view(conn, state))[0]
    conn.close()
    assert "expert queue" in p.next_step.lower()
    assert "new obligation" in p.next_step.lower()


def test_orientation_next_step_becomes_ready_to_load(client):
    _resolve_new_obligation_items(client)
    conn = db.connect(app_module.DB_PATH)
    state = events.replay(conn, PROJECT)
    p = app_module.orientation(state, app_module.versions_view(conn, state))[0]
    conn.close()
    assert "expert queue is clear" in p.next_step.lower()
    assert "3" in p.next_step, "names the version ready to load"


def test_orientation_after_v3_has_two_paragraphs_newest_first(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _load_v3(client).status_code == 303

    conn = db.connect(app_module.DB_PATH)
    state = events.replay(conn, PROJECT)
    paragraphs = app_module.orientation(state, app_module.versions_view(conn, state))
    conn.close()

    assert [p.pair for p in paragraphs] == ["v2 to v3", "v1 to v2"]
    assert "final" in " ".join(paragraphs[0].sentences).lower()
    assert paragraphs[1].next_step is None, "only the newest carries the next step"

    # v3 raises its own new-obligation item, so rule 1 still wins over rule 3.
    assert "expert queue" in paragraphs[0].next_step.lower()
    _resolve_new_obligation_items(client)
    conn = db.connect(app_module.DB_PATH)
    state = events.replay(conn, PROJECT)
    after = app_module.orientation(state, app_module.versions_view(conn, state))
    conn.close()
    assert "every version has been loaded" in after[0].next_step.lower()


def test_orientation_reports_a_rejected_citation(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _load_v3(client)
    conn = db.connect(app_module.DB_PATH)
    state = events.replay(conn, PROJECT)
    newest = app_module.orientation(state, app_module.versions_view(conn, state))[0]
    conn.close()
    assert "citation" in " ".join(newest.sentences).lower()


def test_about_link_shows_on_a_first_visit_and_stops_after_work(client):
    conn = db.connect(app_module.DB_PATH)
    state = events.replay(conn, PROJECT)
    assert app_module.orientation(state, app_module.versions_view(conn, state))[0].about_link
    conn.close()

    _resolve_new_obligation_items(client)
    conn = db.connect(app_module.DB_PATH)
    state = events.replay(conn, PROJECT)
    assert not app_module.orientation(
        state, app_module.versions_view(conn, state)
    )[0].about_link, "the link stops once someone has acted"
    conn.close()


def test_review_page_renders_the_banner(client):
    body = client.get(f"/projects/{PROJECT}/review").text
    assert "compared against version 1" in body
    assert "expert queue" in body.lower()


# --- layout (TDD 6.1) -----------------------------------------------------


def test_header_subtitle_is_data_driven(client):
    body = client.get(f"/projects/{PROJECT}/review").text
    assert "Meridian Power &amp; Light" in body or "Meridian Power & Light" in body
    assert "26-0412-RM" in body


@pytest.mark.parametrize("page", ["review", "queue", "audit", "about"])
def test_sidebar_is_on_every_page(client, page):
    body = client.get(f"/projects/{PROJECT}/{page}").text
    assert "Review center" in body
    assert "Expert queue" in body
    assert "About this workspace" in body
    assert "Versions" in body


def test_sidebar_shows_version_status_before_and_after_loading(client, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    body = client.get(f"/projects/{PROJECT}/review").text
    sidebar = body[body.index("<nav"): body.index("</nav>")]
    assert "baseline" in sidebar, "v1 is the baseline"
    assert "not yet loaded" in sidebar, "v3 is not loaded"
    assert "14" in sidebar, "v2 processed, with its change count"

    _load_v3(client)
    sidebar = client.get(f"/projects/{PROJECT}/review").text
    sidebar = sidebar[sidebar.index("<nav"): sidebar.index("</nav>")]
    assert "not yet loaded" not in sidebar
    assert "13" in sidebar


def test_sidebar_shows_open_counts(client):
    conn = db.connect(app_module.DB_PATH)
    state = events.replay(conn, PROJECT)
    owner = len([t for t in state.open_tasks() if t.get("queue") == "owner"])
    expert = len([t for t in state.open_tasks() if t.get("queue") == "expert"])
    conn.close()
    sidebar = client.get(f"/projects/{PROJECT}/audit").text
    sidebar = sidebar[sidebar.index("<nav"): sidebar.index("</nav>")]
    assert str(owner) in sidebar and str(expert) in sidebar


def test_sidebar_load_button_posts_the_next_version(client):
    sidebar = client.get(f"/projects/{PROJECT}/queue").text
    sidebar = sidebar[sidebar.index("<nav"): sidebar.index("</nav>")]
    assert "/versions" in sidebar
    assert 'value="v3"' in sidebar


# --- review ordering (TDD 6.3) -------------------------------------------


def test_material_changes_come_before_the_details_block(client):
    body = client.get(f"/projects/{PROJECT}/review").text
    assert "<details" in body
    first_material = body.index("c:v1-&gt;v2:p12")
    assert first_material < body.index("<details"), "material changes lead"


def test_details_summary_counts_and_reports_citations(client):
    import re

    body = client.get(f"/projects/{PROJECT}/review").text
    summary = re.search(r"<summary[^>]*>(.*?)</summary>", body, re.S).group(1)
    summary = " ".join(re.sub(r"<[^>]+>", " ", summary).split())
    assert "not material" in summary
    assert re.search(r"\d+ changes", summary)
    assert "citation" in summary


def test_verified_badge_and_path_are_still_in_the_material_section(client):
    """The task 15 assertions must still hold after reordering."""
    body = client.get(f"/projects/{PROJECT}/review").text
    material = body[: body.index("<details")]
    assert "verified" in material
    assert "OBL-3 &rarr; PRJ-1 &rarr; DOC-4" in material or \
           "OBL-3 → PRJ-1 → DOC-4" in material


def test_no_tasks_line_states_the_actual_reason(client):
    body = client.get(f"/projects/{PROJECT}/review").text
    assert "not material" in body
    assert "no downstream nodes" in body or "not material" in body


# --- about page (TDD 6.4) ------------------------------------------------


def test_about_page_renders_with_live_values(client):
    r = client.get(f"/projects/{PROJECT}/about")
    assert r.status_code == 200
    body = r.text
    assert "26-0412-RM" in body
    assert "Meridian" in body
    assert "32" in body, "node count from the database"
    assert "21" in body, "edge count"
    assert "2026-05-14" in body, "v2 issued date"
    assert "2027-01-01" in body, "v3 effective date"


def test_about_page_explains_the_traps_and_the_cache(client):
    body = client.get(f"/projects/{PROJECT}/about").text.lower()
    assert "footnote" in body, "the renumbering trap"
    assert "penalty" in body
    assert "cache" in body
    assert "prefilled" in body or "prefill" in body


def test_about_page_counts_cache_entries(client):
    import pathlib

    body = client.get(f"/projects/{PROJECT}/about").text
    live = len(list(pathlib.Path("data/llm_cache").glob("*.json")))
    assert str(live) in body


# --- task 18: version pages (PRD R4.9, TDD 6.1, 6.2, 6.5) ---


def test_version_page_renders_the_text_as_issued(client):
    r = client.get(f"/projects/{PROJECT}/versions/v2")
    assert r.status_code == 200
    body = r.text
    assert "shall complete the interconnection study for a small storage resource" in body
    assert "within thirty (30) business days" in body


def test_version_page_shows_the_header_block(client):
    body = client.get(f"/projects/{PROJECT}/versions/v2").text
    assert "26-0412-RM" in body
    assert "revised proposed" in body
    assert "2026-05-14" in body


def test_version_page_labels_and_anchors_every_paragraph(client):
    import re

    body = client.get(f"/projects/{PROJECT}/versions/v2").text
    assert 'id="v2:p12"' in body
    anchors = re.findall(r'id="(v2:p\d+)"', body)
    assert len(anchors) == 26, "every paragraph of v2 is addressable"
    assert anchors == [f"v2:p{n}" for n in range(1, 27)]
    assert "v2:p12" in body, "the ID is shown, not only used as an anchor"


def test_version_page_paragraph_text_matches_the_database(client):
    conn = db.connect(app_module.DB_PATH)
    row = conn.execute("select text from paragraphs where para_id='v2:p12'").fetchone()
    conn.close()
    body = client.get(f"/projects/{PROJECT}/versions/v2").text
    assert row["text"][:80] in body, "rendered as stored, so it is what the verifier saw"


def test_every_ingested_version_has_a_page(client):
    for version_id in ("v1", "v2", "v3"):
        assert client.get(f"/projects/{PROJECT}/versions/{version_id}").status_code == 200


def test_unknown_version_is_404(client):
    assert client.get(f"/projects/{PROJECT}/versions/v9").status_code == 404


def test_review_quote_reference_links_to_the_paragraph(client):
    """R4.9: the check the verifier does mechanically, one click for a reviewer."""
    body = client.get(f"/projects/{PROJECT}/review").text
    assert f'href="/projects/{PROJECT}/versions/v2#v2:p12"' in body

    material = body[: body.index("<details")]
    assert "#v2:p12" in material, "the link is on the claim, not only in the details"


def test_sidebar_links_every_version(client):
    body = client.get(f"/projects/{PROJECT}/audit").text
    sidebar = body[body.index("<nav"): body.index("</nav>")]
    for version_id in ("v1", "v2", "v3"):
        assert f'href="/projects/{PROJECT}/versions/{version_id}"' in sidebar


def test_sidebar_keeps_the_load_button_only_on_the_next_version(client):
    body = client.get(f"/projects/{PROJECT}/review").text
    sidebar = body[body.index("<nav"): body.index("</nav>")]
    assert sidebar.count('name="version_id"') == 1
    assert 'value="v3"' in sidebar


def test_next_step_renders_under_the_orientation_paragraphs(client):
    """TDD 6.2: the orientation asks the question, the next step answers it."""
    body = client.get(f"/projects/{PROJECT}/review").text
    banner = body[body.index('class="banner"'):]
    banner = banner[: banner.index("</div>")]
    assert "compared against version 1" in banner
    assert "Next:" in banner
    assert banner.index("compared against version 1") < banner.index("Next:")


def test_load_button_is_under_the_orientation_not_above_it(client):
    body = client.get(f"/projects/{PROJECT}/review").text
    assert body.index("compared against version 1") < body.index("Load next version")


def test_version_page_is_reachable_from_a_rejected_citation(client, monkeypatch):
    """A rejected quote is exactly when a reviewer wants the source text."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _load_v3(client)
    body = client.get(f"/projects/{PROJECT}/queue").text
    assert f'href="/projects/{PROJECT}/versions/' in body


# --- unknown task ids (found by the fresh-clone check) ---


UNKNOWN = "c:v1-&gt;v2:+p17:k1:expert"  # the html-escaped form, as a URL would carry it


def _log_length(path=None):
    conn = db.connect(app_module.DB_PATH)
    count = len(events.history(conn, PROJECT))
    conn.close()
    return count


def test_approve_unknown_task_is_404_and_writes_nothing(client):
    before = _log_length()
    assert client.post(f"/tasks/{UNKNOWN}/approve").status_code == 404
    assert _log_length() == before


def test_escalate_unknown_task_is_404_and_writes_nothing(client):
    before = _log_length()
    assert client.post(f"/tasks/{UNKNOWN}/escalate").status_code == 404
    assert _log_length() == before


def test_edit_unknown_task_is_404_and_writes_nothing(client):
    before = _log_length()
    r = client.post(f"/tasks/{UNKNOWN}/edit", data={"obligation_id": "OBL-5"})
    assert r.status_code == 404
    assert _log_length() == before


def test_create_obligation_unknown_task_is_404_and_writes_nothing(client):
    """The one that mattered: it created the node before the task was checked."""
    before = _log_length()
    r = client.post(f"/tasks/{UNKNOWN}/create-obligation", data={
        "node_id": "OBL-99", "name": "Invented", "text": "", "owner": "P-2",
        "source_para": "v2:p17",
    })
    assert r.status_code == 404
    assert _log_length() == before

    conn = db.connect(app_module.DB_PATH)
    assert "OBL-99" not in {n["id"] for n in events.replay(conn, PROJECT).created_nodes}
    conn.close()


def test_link_obligation_unknown_task_is_404_and_writes_nothing(client):
    before = _log_length()
    r = client.post(f"/tasks/{UNKNOWN}/link-obligation",
                    data={"obligation_id": "OBL-12"})
    assert r.status_code == 404
    assert _log_length() == before


def test_an_unknown_task_never_appears_in_replayed_state(client):
    client.post(f"/tasks/{UNKNOWN}/approve")
    conn = db.connect(app_module.DB_PATH)
    assert UNKNOWN not in events.replay(conn, PROJECT).tasks
    conn.close()


# --- fallback create form uses the next free id ---


def test_fallback_create_form_starts_empty_at_the_next_free_id(client):
    """Once linking is on offer, creating is for a separate duty, not a repeat."""
    conn = db.connect(app_module.DB_PATH)
    first = next(t for t in events.replay(conn, PROJECT).open_tasks()
                 if t.get("reason") == "new_obligation")
    conn.close()
    client.post(f"/tasks/{first['task_id']}/create-obligation", data={
        "node_id": pipeline.OBL_12["id"], "name": pipeline.OBL_12["name"],
        "text": pipeline.OBL_12["text"], "owner": pipeline.OBL_12["owner"],
        "source_para": pipeline.OBL_12["source_para"],
    })

    data = _form_defaults(client.get(f"/projects/{PROJECT}/queue").text)
    assert data["node_id"] == "OBL-13", "next free number, not the one that exists"
    assert data["name"] == ""
    assert data["text"] == ""


def test_posting_the_fallback_defaults_makes_no_duplicate_id(client):
    conn = db.connect(app_module.DB_PATH)
    tasks = [t for t in events.replay(conn, PROJECT).open_tasks()
             if t.get("reason") == "new_obligation"]
    conn.close()
    client.post(f"/tasks/{tasks[0]['task_id']}/create-obligation", data={
        "node_id": pipeline.OBL_12["id"], "name": pipeline.OBL_12["name"],
        "text": pipeline.OBL_12["text"], "owner": pipeline.OBL_12["owner"],
        "source_para": pipeline.OBL_12["source_para"],
    })

    data = _form_defaults(client.get(f"/projects/{PROJECT}/queue").text)
    data["name"] = "Separate duty an expert judged distinct"
    r = client.post(f"/tasks/{tasks[1]['task_id']}/create-obligation", data=data,
                    follow_redirects=False)
    assert r.status_code in (302, 303, 307)

    conn = db.connect(app_module.DB_PATH)
    created = [n["id"] for n in events.replay(conn, PROJECT).created_nodes]
    conn.close()
    assert created == ["OBL-12", "OBL-13"], f"duplicate id: {created}"
    assert len(created) == len(set(created))


def test_next_free_id_skips_a_number_a_rollback_freed(client):
    """An id that existed and was rolled back is not handed out again."""
    conn = db.connect(app_module.DB_PATH)
    task = next(t for t in events.replay(conn, PROJECT).open_tasks()
                if t.get("reason") == "new_obligation")
    before = events.replay(conn, PROJECT).last_seq
    conn.close()

    client.post(f"/tasks/{task['task_id']}/create-obligation", data={
        "node_id": "OBL-12", "name": "n", "text": "", "owner": "P-2",
        "source_para": "v2:p17",
    })
    client.post(f"/projects/{PROJECT}/rollback", data={"to_seq": before})

    conn = db.connect(app_module.DB_PATH)
    assert events.replay(conn, PROJECT).created_nodes == [], "rollback dropped it"
    assert app_module.next_obligation_id(conn, PROJECT) == "OBL-13"
    conn.close()


def test_first_item_still_prefills_the_recorded_obligation(client):
    """With nothing created yet there is no link option, so the prefill stands."""
    data = _form_defaults(client.get(f"/projects/{PROJECT}/queue").text)
    assert data["node_id"] == pipeline.OBL_12["id"]
    assert data["name"] == pipeline.OBL_12["name"]


# --- edit form uses an obligation select ---


def _edit_form(html_text, task_id):
    """The edit form markup for one task, whitespace collapsed."""
    marker = f"/tasks/{task_id}/edit"
    seg = html_text[html_text.index(marker):]
    return " ".join(seg[: seg.index("</form>")].split())


def test_edit_form_is_a_select_not_a_text_box(client):
    body = client.get(f"/projects/{PROJECT}/review").text
    assert 'placeholder="OBL-5"' not in body, "the free-text input is gone"
    assert 'name="obligation_id"' in body
    assert "correct link to" in body
    assert "<select" in body


def test_edit_select_lists_obligations_by_id_and_name(client):
    import html as html_mod

    body = client.get(f"/projects/{PROJECT}/review").text
    conn = db.connect(app_module.DB_PATH)
    task = next(t for t in events.replay(conn, PROJECT).open_tasks()
                if t.get("node_id") == "DOC-2")
    conn.close()

    form = html_mod.unescape(_edit_form(body, task["task_id"].replace(">", "&gt;")))
    assert "OBL-5" in form
    assert "Study scope for storage resources" in form, "names, not just ids"
    assert "OBL-1" in form and "OBL-11" in form


def test_edit_select_excludes_obligations_already_linked_to_that_task(client):
    """PRJ-1 is reached from OBL-3 and OBL-4, so neither is a correction for it."""
    import html as html_mod
    import re

    body = client.get(f"/projects/{PROJECT}/review").text
    conn = db.connect(app_module.DB_PATH)
    task = next(t for t in events.replay(conn, PROJECT).open_tasks()
                if t.get("node_id") == "PRJ-1")
    conn.close()
    assert sorted(task["obligation_ids"]) == ["OBL-3", "OBL-4"]

    form = html_mod.unescape(_edit_form(body, task["task_id"].replace(">", "&gt;")))
    options = re.findall(r'<option value="(OBL-\d+)"', form)
    assert "OBL-3" not in options
    assert "OBL-4" not in options
    assert "OBL-5" in options and "OBL-1" in options
    assert len(options) == 9, "eleven obligations less the two already linked"


def test_edit_select_offers_everything_for_a_task_with_no_links(client):
    import html as html_mod
    import re

    body = client.get(f"/projects/{PROJECT}/review").text
    conn = db.connect(app_module.DB_PATH)
    task = next(t for t in events.replay(conn, PROJECT).open_tasks()
                if not t.get("obligation_ids"))
    conn.close()
    form = html_mod.unescape(_edit_form(body, task["task_id"].replace(">", "&gt;")))
    assert len(re.findall(r'<option value="(OBL-\d+)"', form)) == 11


def test_edit_select_includes_an_obligation_created_by_event(client, monkeypatch):
    import html as html_mod

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _resolve_new_obligation_items(client)
    body = client.get(f"/projects/{PROJECT}/review").text
    conn = db.connect(app_module.DB_PATH)
    task = next(t for t in events.replay(conn, PROJECT).open_tasks()
                if t.get("node_id") == "DOC-2")
    conn.close()
    form = html_mod.unescape(_edit_form(body, task["task_id"].replace(">", "&gt;")))
    assert "OBL-12" in form, "the graph grows through events, so the select does too"


def test_posting_a_selected_obligation_still_records_the_override(client):
    """The event and its two keys are unchanged; only the input changed."""
    conn = db.connect(app_module.DB_PATH)
    task = next(t for t in events.replay(conn, PROJECT).open_tasks()
                if t.get("node_id") == "PRJ-1")
    conn.close()

    client.post(f"/tasks/{task['task_id']}/edit", data={"obligation_id": "OBL-5"})

    conn = db.connect(app_module.DB_PATH)
    edited = [e for e in events.history(conn, PROJECT) if e.type == "task_edited"]
    assert len(edited) == 1
    assert edited[0].payload["obligation_id"] == "OBL-5"
    assert edited[0].payload["change_signature"].startswith("edit:")
    assert edited[0].payload["lineage_key"].startswith("lineage:")
    assert events.replay(conn, PROJECT).tasks[task["task_id"]]["status"] == "edited"
    conn.close()
