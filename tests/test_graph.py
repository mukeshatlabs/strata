"""Graph propagation (PRD R3.2). Deterministic, no model.

Driven by the gold `impacts` and `not_impacted` sections. Every edge in the
company graph points toward an obligation or a project, so propagation is a walk
over incoming edges: from the obligation to whatever points at it.
"""

import json

import pytest

from strata import db, graph, ingest
from strata.models import Edge, Event, Node

GOLD = json.load(open("data/gold/gold.json", encoding="utf-8"))
IMPACTS = GOLD["impacts"]
NOT_IMPACTED = GOLD["not_impacted"]


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


def by_node(impacts):
    return {i.node_id: i for i in impacts}


# --- direction ------------------------------------------------------------


def test_every_edge_points_toward_an_obligation_or_project(g):
    """The premise of the reverse walk, asserted rather than assumed."""
    for to_id, arrivals in g.incoming.items():
        assert g.nodes[to_id].type in ("obligation", "project"), to_id
        for from_id, _ in arrivals:
            assert g.nodes[from_id].type in ("project", "document"), from_id


def test_propagate_walks_incoming_edges(g):
    """OBL-3 has no outgoing edges; a forward walk would return nothing."""
    assert all("OBL-3" != from_id for arrivals in g.incoming.values()
               for from_id, _ in arrivals)
    assert graph.propagate(g, "OBL-3")


# --- gold impacts ---------------------------------------------------------


@pytest.mark.parametrize("row", IMPACTS, ids=[r["id"] for r in IMPACTS])
def test_gold_impacts(row, g):
    impacts = by_node(graph.propagate(g, row["from_obligation"]))
    expected = {e["node"]: e for e in row["expected"]}
    assert set(impacts) == set(expected), row["tests"]
    for node_id, want in expected.items():
        assert impacts[node_id].path == want["path"], node_id
        assert impacts[node_id].hops == want["hops"], node_id


def test_im1_two_hop_impact_records_its_route(g):
    """IM-1 trap: DOC-4 is reachable only through PRJ-1."""
    impacts = by_node(graph.propagate(g, "OBL-3"))
    doc4 = impacts["DOC-4"]
    assert doc4.path == ["OBL-3", "PRJ-1", "DOC-4"]
    assert doc4.hops == 2
    assert doc4.edge_types == ["depends_on", "references"]
    assert doc4.node_type == "document"
    assert doc4.owner == "P-5"


def test_one_hop_impacts_carry_their_edge_type(g):
    impacts = by_node(graph.propagate(g, "OBL-3"))
    assert impacts["PRJ-1"].edge_types == ["depends_on"]
    assert impacts["DOC-1"].edge_types == ["implements"]
    assert impacts["PRJ-1"].owner == "P-2"


def test_im5_obligation_with_no_edges_has_no_impacts(conn):
    """A node created during review has no edges yet."""
    g = graph.load(conn, [new_obligation_event()])
    assert graph.propagate(g, "OBL-12") == []


@pytest.mark.parametrize("row", NOT_IMPACTED, ids=[r["id"] for r in NOT_IMPACTED])
def test_gold_not_impacted(row, g):
    obligation = {"CH-7": "OBL-3", "CH-15": "OBL-6"}[row["change"]]
    reached = {i.node_id for i in graph.propagate(g, obligation)}
    assert reached.isdisjoint(row["nodes"]), row["tests"]


def test_ch7_touches_both_obligations_independently(g):
    """IM-1 and IM-2: PRJ-1 and DOC-1 are reached from OBL-3 and from OBL-4."""
    from_3 = by_node(graph.propagate(g, "OBL-3"))
    from_4 = by_node(graph.propagate(g, "OBL-4"))
    assert {"PRJ-1", "DOC-1"} <= set(from_3) & set(from_4)
    assert from_3["PRJ-1"].path == ["OBL-3", "PRJ-1"]
    assert from_4["PRJ-1"].path == ["OBL-4", "PRJ-1"]
    assert "DOC-2" in from_3 and "DOC-2" not in from_4


# --- events grow the graph ------------------------------------------------


def new_obligation_event():
    """The node_created payload shape defined in TDD 2.5."""
    return Event(
        seq=41,
        ts="2026-05-20T09:00:00+00:00",
        actor="expert",
        type="node_created",
        subject_id="OBL-12",
        payload={
            "id": "OBL-12",
            "type": "obligation",
            "name": "Penalty for missed study deadline",
            "text": "Pay $500 per business day for each day a study is late.",
            "owner": "P-3",
            "source_para": "v2:p17",
        },
    )


def new_edge_event(from_id, to_id, edge_type):
    """The edge_created payload shape defined in TDD 2.5."""
    return Event(
        seq=42,
        ts="2026-05-20T09:01:00+00:00",
        actor="expert",
        type="edge_created",
        subject_id=f"{from_id}->{to_id}",
        payload={"from": from_id, "to": to_id, "type": edge_type},
    )


def test_node_created_event_adds_a_node(conn):
    g = graph.load(conn, [new_obligation_event()])
    assert "OBL-12" in g.nodes
    node = g.nodes["OBL-12"]
    assert node.type == "obligation"
    assert node.owner == "P-3"
    assert node.attrs["source_para"] == "v2:p17"
    assert conn.execute(
        "select count(*) from nodes where node_id='OBL-12'"
    ).fetchone()[0] == 0, "the event log is the source, the table is not written"


def test_node_added_by_event_is_reachable(conn):
    """CH-17 links to OBL-12, created during v2 review."""
    events = [
        new_obligation_event(),
        new_edge_event("DOC-1", "OBL-12", "implements"),
        new_edge_event("PRJ-1", "OBL-12", "depends_on"),
    ]
    g = graph.load(conn, events)
    impacts = by_node(graph.propagate(g, "OBL-12"))
    assert set(impacts) == {"DOC-1", "PRJ-1", "DOC-4"}
    assert impacts["DOC-4"].path == ["OBL-12", "PRJ-1", "DOC-4"]
    assert impacts["DOC-1"].edge_types == ["implements"]


def test_edge_created_to_a_missing_node_is_ignored(conn):
    g = graph.load(conn, [new_edge_event("DOC-1", "OBL-99", "implements")])
    assert graph.propagate(g, "OBL-99") == []


def test_events_do_not_modify_the_json_file(conn):
    before = open("data/company/meridian.json", encoding="utf-8").read()
    graph.load(conn, [new_obligation_event(), new_edge_event("DOC-1", "OBL-12", "implements")])
    assert open("data/company/meridian.json", encoding="utf-8").read() == before


def test_unrelated_events_are_ignored(conn):
    noise = Event(seq=1, ts="t", actor="system", type="claim_verified",
                  subject_id="c:v1->v2:p12:k1", payload={"status": "verified"})
    assert len(graph.load(conn, [noise]).nodes) == len(graph.load(conn, []).nodes)


# --- guards ---------------------------------------------------------------


def test_cycle_guard(conn):
    """A synthetic loop must terminate and visit each node once."""
    events = [
        Event(seq=1, ts="t", actor="expert", type="node_created", subject_id="OBL-99",
              payload={"id": "OBL-99", "type": "obligation", "name": "loop",
                       "text": "", "owner": "P-1", "source_para": None}),
        new_edge_event("PRJ-1", "OBL-99", "depends_on"),
        new_edge_event("DOC-4", "PRJ-1", "references"),
        Event(seq=4, ts="t", actor="expert", type="edge_created", subject_id="loop",
              payload={"from": "OBL-99", "to": "DOC-4", "type": "implements"}),
    ]
    g = graph.load(conn, events)
    impacts = graph.propagate(g, "OBL-99")
    node_ids = [i.node_id for i in impacts]
    assert len(node_ids) == len(set(node_ids)), "a node was visited twice"
    assert "OBL-99" not in node_ids, "the starting node is not its own impact"


def test_max_depth_is_respected(g):
    assert {i.node_id for i in graph.propagate(g, "OBL-3", max_depth=1)} == {
        "PRJ-1", "DOC-1", "DOC-2"
    }
    assert graph.propagate(g, "OBL-3", max_depth=0) == []


def test_shortest_path_wins_when_a_node_is_reachable_twice(conn):
    """DOC-4 reaches OBL-3 directly and through PRJ-1; BFS records the one hop."""
    g = graph.load(conn, [new_edge_event("DOC-4", "OBL-3", "implements")])
    doc4 = by_node(graph.propagate(g, "OBL-3"))["DOC-4"]
    assert doc4.hops == 1
    assert doc4.path == ["OBL-3", "DOC-4"]


def test_unknown_obligation_returns_no_impacts(g):
    assert graph.propagate(g, "OBL-does-not-exist") == []
