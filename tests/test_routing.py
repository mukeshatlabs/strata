"""Escalation rules and task creation (PRD R3.3, R4.3). Driven by gold `routing`."""

import json

import pytest

from strata import db, graph, ingest, routing
from strata.models import Claim, Impact, Link, Verification

GOLD = json.load(open("data/gold/gold.json", encoding="utf-8"))
ROUTING = GOLD["routing"]

CHANGE_OBLIGATIONS = {"CH-7": ["OBL-3", "OBL-4"], "CH-15": ["OBL-6"], "CH-16": ["OBL-7"]}
CHANGE_KIND = {"CH-7": "modified", "CH-15": "removed", "CH-16": "modified", "CH-8": "created"}


@pytest.fixture(scope="module")
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    ingest.ingest_company(c, "data/company/meridian.json")
    yield c
    c.close()


@pytest.fixture(scope="module")
def g(conn):
    return graph.load(conn, [])


def claim(obligation_change="modified", claim_id="c1"):
    return Claim(
        claim_id=claim_id, change_id="ch", material=True, version_status="draft",
        obligation_change=obligation_change, summary="s", quote="q",
        quote_para_id="v2:p12", model_confidence=0.9,
    )


def verified(status="verified", reason=None):
    return Verification(claim_id="c1", status=status, match_start=0, match_end=5,
                        edit_distance=0, overlaps_change=True, reason=reason)


def link(obligation_id="OBL-3", confidence=0.95, rationale_status="verified"):
    return Link(claim_id="c1", obligation_id=obligation_id, confidence=confidence,
                rationale="r", rationale_quote="q", rationale_status=rationale_status)


def tasks_for(change, g, obligation_change=None, links=None, verification=None):
    """Build the tasks a gold routing row implies, the way the pipeline does.

    A paragraph altering two obligations produces ONE CLAIM PER OBLIGATION, not
    one claim with two links: that is what extraction actually returns for CH-7.
    Each claim's tasks are created separately and then merged for the change, so
    these tests exercise the cross-claim merge rather than the within-claim one.
    """
    kind = obligation_change or CHANGE_KIND[change]
    obligations = CHANGE_OBLIGATIONS.get(change, [])
    built = []
    if links is not None:
        c = claim(kind, claim_id="ch:k1")
        impacts = [i for l in links for i in graph.propagate(g, l.obligation_id)]
        built = routing.create_tasks(c, verification or verified(), links, impacts, g)
    else:
        for index, obligation in enumerate(obligations, start=1):
            c = claim(kind, claim_id=f"ch:k{index}")
            ls = [link(obligation)]
            impacts = graph.propagate(g, obligation)
            built.extend(
                routing.create_tasks(c, verification or verified(), ls, impacts, g)
            )
        if not obligations:
            c = claim(kind, claim_id="ch:k1")
            built = routing.create_tasks(c, verification or verified(), [], [], g)
    return routing.merge_tasks(built)


# --- rule order -----------------------------------------------------------


def test_rule_1_rejected_verification_escalates_with_its_reason():
    queue, reason = routing.route(
        claim(), verified("rejected", "wrong_paragraph"), [link()]
    )
    assert (queue, reason) == ("expert", "wrong_paragraph")


def test_rule_2_rejected_rationale_escalates():
    queue, reason = routing.route(
        claim(), verified(), [link(rationale_status="rejected")]
    )
    assert (queue, reason) == ("expert", "rationale_unverified")


def test_rule_2_runs_after_verification_and_before_confidence():
    """A claim failing rules 1, 2 and 4 reports the verification reason."""
    queue, reason = routing.route(
        claim(),
        verified("rejected", "quote_not_found"),
        [link(confidence=0.1, rationale_status="rejected")],
    )
    assert reason == "quote_not_found"

    queue, reason = routing.route(
        claim(), verified(), [link(confidence=0.1, rationale_status="rejected")]
    )
    assert reason == "rationale_unverified", "rationale outranks confidence"


def test_rule_2_tolerates_a_near_rationale():
    queue, _ = routing.route(claim(), verified(), [link(rationale_status="near")])
    assert queue == "owner"


def test_rule_3_created_escalates_as_new_obligation():
    assert routing.route(claim("created"), verified(), []) == ("expert", "new_obligation")


def test_rule_4_low_confidence_escalates():
    queue, reason = routing.route(claim(), verified(), [link(confidence=0.69)])
    assert (queue, reason) == ("expert", "low_confidence")


def test_threshold_boundary_is_inclusive():
    assert routing.THRESHOLD == 0.7
    assert routing.route(claim(), verified(), [link(confidence=0.7)])[0] == "owner"
    assert routing.route(claim(), verified(), [link(confidence=0.699)])[0] == "expert"


def test_rule_5_modified_with_no_links_escalates():
    assert routing.route(claim("modified"), verified(), []) == (
        "expert", "no_confident_link"
    )
    assert routing.route(claim("removed"), verified(), []) == (
        "expert", "no_confident_link"
    )


def test_rule_6_otherwise_owner():
    assert routing.route(claim(), verified(), [link()]) == ("owner", None)


def test_near_verification_is_not_escalated():
    """PRD 9: near-match citations are shown with a badge, not routed to review."""
    assert routing.route(claim(), verified("near"), [link()])[0] == "owner"


# --- action table ---------------------------------------------------------


def test_action_table_covers_every_node_type():
    for change in ("modified", "removed"):
        for node_type in ("obligation", "project", "document"):
            assert (change, node_type) in routing.ACTIONS
    assert ("created", "obligation") in routing.ACTIONS


def test_tdd_wording_is_preserved():
    assert routing.ACTIONS[("modified", "document")] == (
        "Update the document to reflect the changed requirement"
    )
    assert routing.ACTIONS[("removed", "project")] == (
        "Review whether the project is still required"
    )


# --- gold rows ------------------------------------------------------------


@pytest.mark.parametrize("row", ROUTING, ids=[r["id"] for r in ROUTING])
def test_gold_routing_row(row, g):
    tasks = {t.node_id: t for t in tasks_for(row["change"], g)}

    if "node" not in row:
        expert = [t for t in tasks.values() if t.queue == "expert"]
        assert len(expert) == 1
        assert expert[0].reason == row["expected_reason"]
        assert expert[0].queue == row["expected_queue"]
        return

    task = tasks[row["node"]]
    assert task.assignee == row["expected_assignee"], row["id"]
    key = tuple(row["expected_action_key"])
    assert task.recommended_action == routing.ACTIONS[key], row["id"]
    assert task.queue == "owner"


def test_obligation_itself_gets_a_task(g):
    """propagate excludes the start node, so the obligation needs its own task."""
    tasks = {t.node_id: t for t in tasks_for("CH-7", g)}
    assert {"OBL-3", "OBL-4"} <= set(tasks)
    assert tasks["OBL-3"].assignee == "P-2"
    assert tasks["OBL-3"].recommended_action == routing.ACTIONS[("modified", "obligation")]
    assert tasks["OBL-3"].paths == [["OBL-3"]]


def test_removed_obligation_gets_the_retire_action(g):
    tasks = {t.node_id: t for t in tasks_for("CH-15", g)}
    assert tasks["OBL-6"].recommended_action == routing.ACTIONS[("removed", "obligation")]
    assert tasks["OBL-6"].assignee == "P-3"


# --- dedupe ---------------------------------------------------------------


def test_one_task_per_node_when_two_obligations_reach_it(g):
    """IM-2: PRJ-1 and DOC-1 are reached from OBL-3 and OBL-4, by separate claims."""
    tasks = tasks_for("CH-7", g)
    node_ids = [t.node_id for t in tasks]
    assert len(node_ids) == len(set(node_ids)), "duplicate task for one node"
    assert node_ids.count("PRJ-1") == 1
    assert node_ids.count("DOC-1") == 1


def test_every_path_that_reached_a_node_is_kept(g):
    tasks = {t.node_id: t for t in tasks_for("CH-7", g)}
    assert tasks["PRJ-1"].paths == [["OBL-3", "PRJ-1"], ["OBL-4", "PRJ-1"]]
    assert sorted(tasks["PRJ-1"].obligation_ids) == ["OBL-3", "OBL-4"]

    assert tasks["DOC-4"].paths == [
        ["OBL-3", "PRJ-1", "DOC-4"],
        ["OBL-4", "PRJ-1", "DOC-4"],
    ]
    assert tasks["DOC-2"].paths == [["OBL-3", "DOC-2"]], "only OBL-3 reaches DOC-2"


def test_task_ids_are_unique_and_name_the_change_and_node(g):
    tasks = tasks_for("CH-7", g)
    ids = [t.task_id for t in tasks]
    assert len(ids) == len(set(ids))
    assert all(t.node_id in t.task_id for t in tasks)
    assert all(t.task_id.startswith(t.change_id) for t in tasks)


def test_a_merged_task_records_every_claim_behind_it(g):
    """Approving one merged task must accept both claims that produced it."""
    tasks = {t.node_id: t for t in tasks_for("CH-7", g)}
    assert sorted(tasks["PRJ-1"].claim_ids) == ["ch:k1", "ch:k2"]
    assert tasks["DOC-2"].claim_ids == ["ch:k1"], "only OBL-3's claim reaches DOC-2"


def test_expert_tasks_are_not_merged(g):
    """Each escalated claim keeps its own reason."""
    one = routing.create_tasks(claim("created", "ch:k1"), verified(), [], [], g)
    two = routing.create_tasks(claim("created", "ch:k2"), verified(), [], [], g)
    merged = routing.merge_tasks(one + two)
    assert len(merged) == 2
    assert {t.queue for t in merged} == {"expert"}


# --- escalated claims -----------------------------------------------------


def test_escalated_claim_makes_one_task_with_no_node(g):
    tasks = tasks_for("CH-8", g, obligation_change="created", links=[])
    assert len(tasks) == 1
    task = tasks[0]
    assert task.queue == "expert"
    assert task.reason == "new_obligation"
    assert task.node_id is None
    assert task.assignee == routing.EXPERT == "P-8", "regulatory counsel"
    assert task.recommended_action == routing.ACTIONS[("created", "obligation")]


def test_escalation_makes_no_owner_tasks_even_with_impacts(g):
    """A low-confidence claim must not reach owners before a human agrees."""
    tasks = tasks_for("CH-7", g, links=[link("OBL-3", confidence=0.4)])
    assert [t.queue for t in tasks] == ["expert"]
    assert all(t.node_id is None for t in tasks)


def test_rejected_verification_carries_its_reason_onto_the_task(g):
    tasks = tasks_for(
        "CH-7", g, verification=verified("rejected", "quote_outside_changed_span")
    )
    assert tasks[0].reason == "quote_outside_changed_span"


def test_tasks_start_open(g):
    assert all(t.status == "open" for t in tasks_for("CH-7", g))


def test_claim_with_no_impacts_and_no_links_makes_no_tasks(g):
    assert routing.create_tasks(claim("none"), verified(), [], [], g) == []
