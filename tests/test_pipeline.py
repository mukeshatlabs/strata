"""The core loop (PRD section 4 workflow).

Two layers. The stubbed tests drive sequencing, the override check, the
created-claim skip, the event writes and dedupe without inventing responses for
every change. The end-to-end test runs the real cache and skips with a clear
message until task 13 has populated it.
"""

import json

import pytest

from strata import db, events, ingest, llm, pipeline
from strata.models import Change

GOLD = json.load(open("data/gold/gold.json", encoding="utf-8"))
PROJECT = "proj-1"

NO_CLAIMS = {"version_status": "draft", "claims": []}
CH7_CLAIMS = {
    "version_status": "draft",
    "claims": [
        {
            "material": True,
            "obligation_change": "modified",
            "summary": "Study deadline shortened from 45 to 30 business days.",
            "quote": "shall complete the interconnection study for a small storage resource within thirty (30) business days",
            "quote_para_id": "v2:p12",
            "confidence": 0.94,
        }
    ],
}
CH7_LINKS = {
    "links": [
        {
            "obligation_id": "OBL-3",
            "confidence": 0.96,
            "rationale": "Both govern the study completion deadline.",
            "rationale_quote": "within forty-five (45) business days",
        }
    ]
}


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    for v in ("v1", "v2", "v3"):
        ingest.ingest_version(c, f"data/proceeding/{v}.md")
    ingest.ingest_company(c, "data/company/meridian.json")
    yield c
    c.close()


def stub_llm(monkeypatch, extract_for_ch7=CH7_CLAIMS, mapping_response=CH7_LINKS,
             calls=None):
    """Answer for CH-7 only; everything else is a no-claims response."""

    def fake_call(name, messages, schema, prompt_version):
        content = messages[0]["content"]
        if calls is not None:
            calls.append((name, prompt_version))
        if name == "propose_links":
            return mapping_response
        if "New version, paragraph [v2:p12]:" in content:
            return extract_for_ch7
        return NO_CLAIMS

    monkeypatch.setattr(llm, "call", fake_call)


# --- sequencing -----------------------------------------------------------


def test_run_version_returns_a_summary(conn, monkeypatch):
    stub_llm(monkeypatch)
    run = pipeline.run_version(conn, PROJECT, "v2")
    assert run["from_version"] == "v1"
    assert run["to_version"] == "v2"
    assert run["changes"] == 14
    assert run["claims"] == 1
    assert run["tasks"]


def test_first_version_has_nothing_to_diff(conn, monkeypatch):
    stub_llm(monkeypatch)
    run = pipeline.run_version(conn, PROJECT, "v1")
    assert run["changes"] == 0
    assert run["tasks"] == []
    assert events.history(conn, PROJECT) == []


def test_stages_run_in_order(conn, monkeypatch):
    calls = []
    stub_llm(monkeypatch, calls=calls)
    pipeline.run_version(conn, PROJECT, "v2")
    names = [c[0] for c in calls]
    assert names.count("propose_links") == 1
    where = names.index("propose_links")
    assert names[where - 1] == "extract_claims", "mapping follows its own extraction"
    assert "extract_claims" in names[where + 1:], "later changes are still extracted"
    assert calls[where][1] == "mapping/v1"


def test_every_stage_writes_an_event(conn, monkeypatch):
    stub_llm(monkeypatch)
    pipeline.run_version(conn, PROJECT, "v2")
    types = [e.type for e in events.history(conn, PROJECT)]
    assert types.count("claim_extracted") == 1
    assert types.count("claim_verified") == 1
    assert types.count("link_proposed") == 1
    assert types.count("impact_found") == 4
    assert types.count("task_created") == 5, "OBL-3 plus its four downstream nodes"
    assert all(e.actor == "system" for e in events.history(conn, PROJECT))


def test_events_carry_their_subject(conn, monkeypatch):
    stub_llm(monkeypatch)
    pipeline.run_version(conn, PROJECT, "v2")
    extracted = [e for e in events.history(conn, PROJECT) if e.type == "claim_extracted"]
    assert extracted[0].subject_id == "c:v1->v2:p12:k1"
    assert extracted[0].payload["change_id"] == "c:v1->v2:p12"


def test_tasks_are_replayable_into_state(conn, monkeypatch):
    stub_llm(monkeypatch)
    pipeline.run_version(conn, PROJECT, "v2")
    state = events.replay(conn, PROJECT)
    assert len(state.open_tasks()) == 5
    assert state.accepted_claims == {}, "nothing is accepted without a human"


# --- dedupe ---------------------------------------------------------------


def test_one_task_per_node_across_two_obligations(conn, monkeypatch):
    """CH-7 from the real cache: two claims, one per obligation, one task per node.

    Driven by the committed responses rather than a stub, because the stub shape
    is what hid this: extraction returns one claim per obligation altered, so
    the merge has to happen across a change's claims, not inside one claim.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    run = pipeline.run_version(conn, PROJECT, "v2")

    ch7 = [t for t in run["tasks"] if t.change_id == "c:v1->v2:p12"]
    claims = [c for c in events.replay(conn, PROJECT).claims.values()
              if c["change_id"] == "c:v1->v2:p12"]
    assert len(claims) == 2, "CH-7 alters two obligations, so it yields two claims"

    node_ids = [t.node_id for t in ch7]
    assert len(node_ids) == len(set(node_ids)), f"duplicate node tasks: {node_ids}"
    assert set(node_ids) == {"OBL-3", "OBL-4", "PRJ-1", "DOC-1", "DOC-2", "DOC-4"}

    prj1 = next(t for t in ch7 if t.node_id == "PRJ-1")
    assert prj1.paths == [["OBL-3", "PRJ-1"], ["OBL-4", "PRJ-1"]]
    assert sorted(prj1.claim_ids) == ["c:v1->v2:p12:k1", "c:v1->v2:p12:k2"]
    assert prj1.task_id == "c:v1->v2:p12:PRJ-1"


def test_approving_a_merged_task_accepts_both_claims(conn, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    run = pipeline.run_version(conn, PROJECT, "v2")
    prj1 = next(t for t in run["tasks"] if t.node_id == "PRJ-1")

    events.append(conn, events.Event(
        seq=None, ts="t", actor="dana", type="task_approved", subject_id=prj1.task_id,
        payload={"task_id": prj1.task_id, "claim_id": prj1.claim_id,
                 "claim_ids": prj1.claim_ids},
        project_id=PROJECT,
    ))
    accepted = events.replay(conn, PROJECT).accepted_claims
    assert set(prj1.claim_ids) <= set(accepted)


# --- the created-claim skip -----------------------------------------------


def test_created_claim_skips_the_mapping_call(conn, monkeypatch):
    created = {
        "version_status": "draft",
        "claims": [{
            "material": True, "obligation_change": "created",
            "summary": "New penalty for a missed study deadline.",
            "quote": "Penalties.", "quote_para_id": "v2:p17", "confidence": 0.9,
        }],
    }
    calls = []

    def fake_call(name, messages, schema, prompt_version):
        calls.append(name)
        if name == "propose_links":
            raise AssertionError("mapping must not be called for a created claim")
        content = messages[0]["content"]
        marker = "New version, paragraph [v2:p17]:"
        return created if marker in content else NO_CLAIMS

    monkeypatch.setattr(llm, "call", fake_call)
    run = pipeline.run_version(conn, PROJECT, "v2")

    assert "propose_links" not in calls
    expert = [t for t in run["tasks"] if t.queue == "expert"]
    assert len(expert) == 1
    assert expert[0].reason == "new_obligation"
    assert expert[0].assignee == "P-8"


# --- materiality and rejected citations -----------------------------------


def test_non_material_claim_makes_no_tasks(conn, monkeypatch):
    immaterial = {
        "version_status": "draft",
        "claims": [{
            "material": False, "obligation_change": "none", "summary": "Wording.",
            "quote": "thirty (30) ", "quote_para_id": "v2:p12", "confidence": 0.8,
        }],
    }
    stub_llm(monkeypatch, extract_for_ch7=immaterial)
    run = pipeline.run_version(conn, PROJECT, "v2")
    assert run["tasks"] == []
    types = [e.type for e in events.history(conn, PROJECT)]
    assert "claim_verified" in types, "it is still verified and recorded"
    assert "link_proposed" not in types


def test_rejected_citation_escalates_before_the_materiality_gate(conn, monkeypatch):
    bad_quote = {
        "version_status": "draft",
        "claims": [{
            "material": False, "obligation_change": "none",
            "summary": "Invented.", "quote": "the study deadline is now shorter",
            "quote_para_id": "v2:p12", "confidence": 0.8,
        }],
    }
    stub_llm(monkeypatch, extract_for_ch7=bad_quote)
    run = pipeline.run_version(conn, PROJECT, "v2")
    assert [t.queue for t in run["tasks"]] == ["expert"]
    assert run["tasks"][0].reason == "quote_not_found"


# --- overrides ------------------------------------------------------------


def test_override_is_applied_instead_of_calling_the_model(conn, monkeypatch):
    """PRD R4.4: a mapping corrected earlier is not re-proposed."""
    from strata import diff

    change = next(
        c for c in diff.diff_versions(conn, "v1", "v2") if c.change_id == "c:v1->v2:p12"
    )
    events.append(conn, events.Event(
        seq=None, ts="t", actor="dana", type="task_edited", subject_id="t1",
        payload={
            "task_id": "t1",
            "change_signature": events.signature(change),
            "lineage_key": events.lineage_key(change.text_to),
            "obligation_id": "OBL-5",
            "correction": {"obligation_id": "OBL-5", "confidence": 1.0,
                           "rationale": "Dana corrected this to the study scope duty."},
        },
        project_id=PROJECT,
    ))

    calls = []
    stub_llm(monkeypatch, calls=calls)
    run = pipeline.run_version(conn, PROJECT, "v2")

    assert "propose_links" not in [c[0] for c in calls], "the model was asked anyway"
    assert any(t.node_id == "OBL-5" for t in run["tasks"])
    assert not any(t.node_id == "OBL-3" for t in run["tasks"])


def test_obligation_created_by_event_is_a_mapping_candidate(conn, monkeypatch):
    """CH-17 links to OBL-12, created during v2 review."""
    events.append(conn, events.Event(
        seq=None, ts="t", actor="expert", type="node_created", subject_id="OBL-12",
        payload={"id": "OBL-12", "type": "obligation", "name": "Penalty",
                 "text": "Pay $500 per business day for a late study.",
                 "owner": "P-3", "source_para": "v2:p17"},
        project_id=PROJECT,
    ))
    seen = {}

    def fake_call(name, messages, schema, prompt_version):
        if name == "propose_links":
            seen["prompt"] = messages[0]["content"]
            return {"links": []}
        content = messages[0]["content"]
        marker = "New version, paragraph [v2:p12]:"
        return CH7_CLAIMS if marker in content else NO_CLAIMS

    monkeypatch.setattr(llm, "call", fake_call)
    pipeline.run_version(conn, PROJECT, "v2")
    assert "OBL-12" in seen["prompt"]


# --- end to end against the real cache ------------------------------------


def test_v1_to_v2_end_to_end_from_cache(conn, monkeypatch):
    """The real run. Skips until task 13 records the responses."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    try:
        run = pipeline.run_version(conn, PROJECT, "v2")
    except llm.CacheMiss as miss:
        pytest.skip(
            f"cache not yet populated for the full v1->v2 run; task 13 records it. {miss}"
        )

    tasks = {t.node_id: t for t in run["tasks"] if t.queue == "owner"}
    for node_id in ("PRJ-1", "DOC-1", "DOC-2", "DOC-4"):
        assert node_id in tasks, f"gold routing expects a task for {node_id}"

    for row in [r for r in GOLD["routing"] if r["change"] == "CH-7" and "node" in r]:
        assert tasks[row["node"]].assignee == row["expected_assignee"], row["id"]

    expert = [t for t in run["tasks"] if t.queue == "expert"]
    assert any(t.reason == "new_obligation" for t in expert), "CH-8 must escalate"

    types = [e.type for e in events.history(conn, PROJECT)]
    assert types.count("task_created") == len(run["tasks"])
    assert "claim_verified" in types


def test_a_failed_run_writes_nothing(conn, monkeypatch):
    """run_version is all or nothing: a mid-run failure leaves no partial claims."""
    calls = []

    def fake_call(name, messages, schema, prompt_version):
        calls.append(name)
        if len(calls) > 4:
            raise llm.CacheMiss("simulated miss part way through the run")
        return NO_CLAIMS if "New version, paragraph [v2:p12]:" not in messages[0]["content"] \
            else CH7_CLAIMS

    monkeypatch.setattr(llm, "call", fake_call)
    with pytest.raises(llm.CacheMiss):
        pipeline.run_version(conn, PROJECT, "v2")

    assert events.history(conn, PROJECT) == [], "partial events survived the failure"
    assert events.replay(conn, PROJECT).claims == {}
