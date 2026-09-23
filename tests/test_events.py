"""Append-only log, replay, rollback, overrides (PRD R4.1, R4.2, R4.4)."""

import pytest

from strata import db, events, graph, ingest
from strata.models import Change, Event

PROJECT = "proj-1"


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    ingest.ingest_company(c, "data/company/meridian.json")
    yield c
    c.close()


def change(text_from="Complete the study within forty-five (45) business days.",
           text_to="Complete the study within thirty (30) business days.",
           kind="modified", change_id="c:v1->v2:p12"):
    return Change(
        change_id=change_id, from_version="v1", to_version="v2",
        para_id_from="v1:p12", para_id_to="v2:p12", kind=kind,
        spans_from=[(0, 5)], spans_to=[(0, 5)],
        text_from=text_from, text_to=text_to,
    )


def event(type, subject_id="s", payload=None, actor="system"):
    return Event(seq=None, ts="2026-05-14T10:00:00+00:00", actor=actor, type=type,
                 subject_id=subject_id, payload=payload or {}, project_id=PROJECT)


def edit_event(ch, obligation_id, correction, task_id="t1"):
    """A human correcting a mapping (TDD 3.9)."""
    return event(
        "task_edited",
        subject_id=task_id,
        actor="dana",
        payload={
            "task_id": task_id,
            "change_signature": events.signature(ch),
            "lineage_key": events.lineage_key(ch.text_to),
            "obligation_id": obligation_id,
            "correction": correction,
        },
    )


# --- append ---------------------------------------------------------------


def test_append_assigns_sequence_numbers(conn):
    a = events.append(conn, event("claim_extracted", "k1"))
    b = events.append(conn, event("claim_verified", "k1"))
    assert (a.seq, b.seq) == (1, 2)


def test_append_records_actor_and_timestamp(conn):
    events.append(conn, event("task_approved", "t1", actor="dana"))
    row = conn.execute("select * from events").fetchone()
    assert row["actor"] == "dana"
    assert row["ts"].startswith("2026-05-14")
    assert row["project_id"] == PROJECT


def test_append_is_the_only_write_and_never_updates(conn):
    events.append(conn, event("task_created", "t1", {"status": "open"}))
    events.append(conn, event("task_approved", "t1"))
    assert conn.execute("select count(*) from events").fetchone()[0] == 2


# --- replay ---------------------------------------------------------------


def test_replay_folds_events_into_state(conn):
    events.append(conn, event("claim_extracted", "k1", {"claim_id": "k1", "material": True}))
    events.append(conn, event("claim_verified", "k1", {"claim_id": "k1", "status": "verified"}))
    events.append(conn, event("task_created", "t1", {"task_id": "t1", "claim_id": "k1",
                                                     "node_id": "PRJ-1", "queue": "owner"}))
    state = events.replay(conn, PROJECT)
    assert state.claims["k1"]["material"] is True
    assert state.verifications["k1"]["status"] == "verified"
    assert state.tasks["t1"]["status"] == "open"
    assert state.last_seq == 3


def test_task_status_follows_the_last_human_action(conn):
    events.append(conn, event("task_created", "t1", {"task_id": "t1", "claim_id": "k1"}))
    events.append(conn, event("task_escalated", "t1", {"task_id": "t1"}, actor="dana"))
    events.append(conn, event("task_approved", "t1", {"task_id": "t1"}, actor="expert"))
    state = events.replay(conn, PROJECT)
    assert state.tasks["t1"]["status"] == "approved"
    assert state.open_tasks() == []
    assert [t["task_id"] for t in state.closed_tasks()] == ["t1"]


def test_a_claim_is_accepted_only_after_a_human_approves(conn):
    events.append(conn, event("claim_extracted", "k1", {"claim_id": "k1"}))
    events.append(conn, event("task_created", "t1", {"task_id": "t1", "claim_id": "k1"}))
    assert events.replay(conn, PROJECT).accepted_claims == {}

    events.append(conn, event("task_approved", "t1", {"task_id": "t1"}, actor="dana"))
    assert "k1" in events.replay(conn, PROJECT).accepted_claims


def test_impacts_are_confirmed_by_approval(conn):
    events.append(conn, event("claim_extracted", "k1", {"claim_id": "k1"}))
    events.append(conn, event("impact_found", "k1", {"claim_id": "k1", "node_id": "DOC-4",
                                                     "path": ["OBL-3", "PRJ-1", "DOC-4"]}))
    events.append(conn, event("task_created", "t1", {"task_id": "t1", "claim_id": "k1",
                                                     "node_id": "DOC-4"}))
    assert events.replay(conn, PROJECT).confirmed_impacts == {}
    events.append(conn, event("task_approved", "t1", {"task_id": "t1"}, actor="dana"))
    state = events.replay(conn, PROJECT)
    assert state.confirmed_impacts["k1"][0]["node_id"] == "DOC-4"


def test_replay_upto_excludes_later_events(conn):
    events.append(conn, event("claim_extracted", "k1", {"claim_id": "k1"}))
    events.append(conn, event("task_created", "t1", {"task_id": "t1", "claim_id": "k1"}))
    events.append(conn, event("task_approved", "t1", {"task_id": "t1"}, actor="dana"))

    assert events.replay(conn, PROJECT, upto=2).tasks["t1"]["status"] == "open"
    assert events.replay(conn, PROJECT, upto=2).accepted_claims == {}
    assert events.replay(conn, PROJECT).tasks["t1"]["status"] == "approved"


def test_replay_ignores_other_projects(conn):
    events.append(conn, event("claim_extracted", "k1", {"claim_id": "k1"}))
    other = event("claim_extracted", "k2", {"claim_id": "k2"})
    events.append(conn, Event(**{**other.__dict__, "project_id": "proj-2"}))
    assert set(events.replay(conn, PROJECT).claims) == {"k1"}


# --- rollback -------------------------------------------------------------


def test_rollback_appends_an_event_and_deletes_nothing(conn):
    events.append(conn, event("claim_extracted", "k1", {"claim_id": "k1"}))
    events.append(conn, event("task_created", "t1", {"task_id": "t1", "claim_id": "k1"}))
    events.append(conn, event("task_approved", "t1", {"task_id": "t1"}, actor="dana"))
    before = conn.execute("select count(*) from events").fetchone()[0]

    rolled = events.rollback(conn, PROJECT, to_seq=2)
    assert rolled.type == "rollback"
    assert rolled.payload["to_seq"] == 2
    assert conn.execute("select count(*) from events").fetchone()[0] == before + 1


def test_replay_honours_a_rollback(conn):
    events.append(conn, event("claim_extracted", "k1", {"claim_id": "k1"}))
    events.append(conn, event("task_created", "t1", {"task_id": "t1", "claim_id": "k1"}))
    events.append(conn, event("task_approved", "t1", {"task_id": "t1"}, actor="dana"))
    events.rollback(conn, PROJECT, to_seq=2)

    state = events.replay(conn, PROJECT)
    assert state.tasks["t1"]["status"] == "open"
    assert state.accepted_claims == {}
    assert state.rolled_back_to == 2


def test_rollback_appears_in_the_audit_list(conn):
    events.append(conn, event("claim_extracted", "k1", {"claim_id": "k1"}))
    events.rollback(conn, PROJECT, to_seq=1, actor="dana")
    log = events.history(conn, PROJECT)
    assert [e.type for e in log] == ["claim_extracted", "rollback"]
    assert log[-1].actor == "dana"


def test_events_after_a_rollback_still_apply(conn):
    events.append(conn, event("claim_extracted", "k1", {"claim_id": "k1"}))
    events.append(conn, event("task_created", "t1", {"task_id": "t1", "claim_id": "k1"}))
    events.append(conn, event("task_approved", "t1", {"task_id": "t1"}, actor="dana"))
    events.rollback(conn, PROJECT, to_seq=2)
    events.append(conn, event("task_escalated", "t1", {"task_id": "t1"}, actor="dana"))
    assert events.replay(conn, PROJECT).tasks["t1"]["status"] == "escalated"


def test_nested_rollbacks(conn):
    for i in range(1, 5):
        events.append(conn, event("task_created", f"t{i}", {"task_id": f"t{i}"}))
    events.rollback(conn, PROJECT, to_seq=3)
    events.rollback(conn, PROJECT, to_seq=2)
    state = events.replay(conn, PROJECT)
    assert set(state.tasks) == {"t1", "t2"}


# --- created nodes and edges ----------------------------------------------


def test_created_nodes_and_edges_reach_state_and_the_graph(conn):
    node = {"id": "OBL-12", "type": "obligation", "name": "Penalty",
            "text": "Pay $500 per business day.", "owner": "P-3", "source_para": "v2:p17"}
    edge = {"from": "DOC-1", "to": "OBL-12", "type": "implements"}
    events.append(conn, event("node_created", "OBL-12", node, actor="expert"))
    events.append(conn, event("edge_created", "DOC-1->OBL-12", edge, actor="expert"))

    state = events.replay(conn, PROJECT)
    assert state.created_nodes == [node]
    assert state.created_edges == [edge]

    g = graph.load(conn, events.history(conn, PROJECT))
    assert "OBL-12" in g.nodes
    assert [i.node_id for i in graph.propagate(g, "OBL-12")] == ["DOC-1"]


def test_a_rolled_back_node_is_not_in_the_graph(conn):
    events.append(conn, event("node_created", "OBL-12",
                              {"id": "OBL-12", "type": "obligation", "name": "n",
                               "text": "", "owner": "P-3", "source_para": None}))
    events.rollback(conn, PROJECT, to_seq=0)
    assert events.replay(conn, PROJECT).created_nodes == []
    assert "OBL-12" not in graph.load(conn, events.effective(conn, PROJECT)).nodes


# --- signatures and overrides ---------------------------------------------


def test_signature_ignores_paragraph_ids_and_versions():
    a = change(change_id="c:v1->v2:p12")
    b = Change(**{**a.__dict__, "change_id": "c:v2->v3:p99", "para_id_from": "v2:p40",
                  "para_id_to": "v3:p7", "from_version": "v2", "to_version": "v3"})
    assert events.signature(a) == events.signature(b)


def test_signature_ignores_whitespace_and_curly_punctuation():
    a = change(text_to="Complete the study within thirty (30) business days.")
    b = change(text_to="Complete  the study within thirty (30) business days.")
    assert events.signature(a) == events.signature(b)


def test_signature_changes_when_the_edit_changes():
    assert events.signature(change()) != events.signature(
        change(text_to="Complete the study within twenty (20) business days.")
    )
    assert events.signature(change()) != events.signature(change(kind="removed"))


def test_override_is_found_by_its_own_signature(conn):
    ch = change()
    events.append(conn, edit_event(ch, "OBL-3", {"obligation_id": "OBL-5"}))
    state = events.replay(conn, PROJECT)
    assert events.override_for(state, ch, "OBL-3") == {"obligation_id": "OBL-5"}


def test_override_is_absent_for_an_unrelated_change(conn):
    events.append(conn, edit_event(change(), "OBL-3", {"obligation_id": "OBL-5"}))
    state = events.replay(conn, PROJECT)
    other = change(text_from="Something else entirely.", text_to="And another thing.")
    assert events.override_for(state, other, "OBL-3") is None
    assert events.override_for(state, change(), "OBL-9") is None


def test_override_carries_into_a_later_edit_of_the_same_paragraph(conn):
    """R4.4 lineage: v2 produced this text, Dana corrected it, v3 edits it again."""
    v2_text = "Complete the study within thirty (30) business days."
    v2_change = change(
        text_from="Complete the study within forty-five (45) business days.",
        text_to=v2_text,
    )
    events.append(conn, edit_event(v2_change, "OBL-3", {"obligation_id": "OBL-5"}))
    state = events.replay(conn, PROJECT)

    v3_change = Change(
        change_id="c:v2->v3:p11", from_version="v2", to_version="v3",
        para_id_from="v2:p12", para_id_to="v3:p11", kind="modified",
        spans_from=[(0, 5)], spans_to=[(0, 5)],
        text_from=v2_text,
        text_to="Complete the study within thirty (30) business days of the application.",
    )
    assert events.signature(v3_change) != events.signature(v2_change)
    assert events.override_for(state, v3_change, "OBL-3") == {"obligation_id": "OBL-5"}


def test_lineage_match_survives_renumbering_and_whitespace(conn):
    v2_text = "Maintain on its public website a queue of pending requests."
    v2_change = change(text_from="Maintain a queue.", text_to=v2_text)
    events.append(conn, edit_event(v2_change, "OBL-6", {"drop": True}))
    state = events.replay(conn, PROJECT)

    v3_change = Change(
        change_id="c:v2->v3:p99", from_version="v2", to_version="v3",
        para_id_from="v2:p14", para_id_to="v3:p88", kind="modified",
        spans_from=[(0, 5)], spans_to=[(0, 5)],
        text_from="Maintain  on its public website a queue of pending requests.",
        text_to="Maintain a semi-annual report.",
    )
    assert events.override_for(state, v3_change, "OBL-6") == {"drop": True}


def test_lineage_does_not_match_a_third_generation_by_accident(conn):
    """The override follows the text it corrected, not every later edit forever."""
    v2_change = change(text_from="A.", text_to="B.")
    events.append(conn, edit_event(v2_change, "OBL-3", {"obligation_id": "OBL-5"}))
    state = events.replay(conn, PROJECT)
    third = change(text_from="C.", text_to="D.")
    assert events.override_for(state, third, "OBL-3") is None


def test_an_override_is_dropped_by_a_rollback(conn):
    ch = change()
    events.append(conn, edit_event(ch, "OBL-3", {"obligation_id": "OBL-5"}))
    events.rollback(conn, PROJECT, to_seq=0)
    assert events.override_for(events.replay(conn, PROJECT), ch, "OBL-3") is None
