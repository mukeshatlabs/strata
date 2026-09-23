"""Company graph and impact propagation (TDD 3.7). Deterministic, no model.

Every edge in the company graph points toward an obligation or a project:
a project depends_on an obligation, a document implements an obligation, a
document references a project. So finding what a changed obligation affects is a
walk over *incoming* edges, from the obligation outward to whatever points at
it. A forward walk from an obligation reaches nothing.

The graph is the JSON file as ingested plus every node_created and edge_created
event since. The file on disk is never written by the application (TDD 3.9).
"""

import json
from collections import deque
from dataclasses import dataclass, field

from .models import Impact, Node

NODE_CREATED = "node_created"
EDGE_CREATED = "edge_created"
NODE_FIELDS = ("id", "type", "name", "text", "owner", "source_para")
EDGE_FIELDS = ("from", "to", "type")


@dataclass(frozen=True)
class Graph:
    """Nodes by ID, and for each node the edges arriving at it."""

    nodes: dict[str, Node] = field(default_factory=dict)
    incoming: dict[str, list[tuple[str, str]]] = field(default_factory=dict)


def _payload(event) -> dict:
    """Return an event's payload, whether it is an Event or a mapping."""
    return event["payload"] if isinstance(event, dict) else event.payload


def _type(event) -> str:
    return event["type"] if isinstance(event, dict) else event.type


def _add_edge(graph: Graph, from_id: str, to_id: str, edge_type: str) -> None:
    """Record an edge, ignoring one whose endpoints are not both known."""
    if from_id not in graph.nodes or to_id not in graph.nodes:
        return
    arrivals = graph.incoming.setdefault(to_id, [])
    if (from_id, edge_type) not in arrivals:
        arrivals.append((from_id, edge_type))


def load(conn, events=()) -> Graph:
    """Build the graph from the nodes and edges tables plus the event log.

    node_created payloads carry id, type, name, text, owner, source_para;
    edge_created payloads carry from, to, type (TDD 2.5).
    """
    graph = Graph()
    for row in conn.execute("select * from nodes order by node_id"):
        graph.nodes[row["node_id"]] = Node(
            node_id=row["node_id"],
            type=row["type"],
            name=row["name"],
            text=row["text"] or "",
            owner=row["owner"],
            attrs=json.loads(row["attrs"]),
            company_id=row["company_id"],
        )
    for row in conn.execute("select * from edges"):
        _add_edge(graph, row["from_id"], row["to_id"], row["type"])

    for event in events:
        kind = _type(event)
        if kind == NODE_CREATED:
            body = _payload(event)
            graph.nodes[body["id"]] = Node(
                node_id=body["id"],
                type=body["type"],
                name=body["name"],
                text=body.get("text") or "",
                owner=body.get("owner"),
                attrs={"source_para": body.get("source_para")},
            )
        elif kind == EDGE_CREATED:
            body = _payload(event)
            _add_edge(graph, body["from"], body["to"], body["type"])
    return graph


def propagate(graph: Graph, obligation_id: str, max_depth: int = 3) -> list[Impact]:
    """Return the nodes reachable from an obligation by incoming edges.

    Breadth-first, so each node is recorded by its shortest route, with the path
    and the edge types along it. A visited set guards against cycles.
    """
    if obligation_id not in graph.nodes:
        return []
    impacts: list[Impact] = []
    seen = {obligation_id}
    queue = deque([(obligation_id, [obligation_id], [])])
    while queue:
        node_id, path, edge_types = queue.popleft()
        if len(path) - 1 >= max_depth:
            continue
        for from_id, edge_type in graph.incoming.get(node_id, []):
            if from_id in seen:
                continue
            seen.add(from_id)
            node = graph.nodes[from_id]
            next_path = path + [from_id]
            next_edges = edge_types + [edge_type]
            impacts.append(
                Impact(
                    obligation_id=obligation_id,
                    node_id=from_id,
                    node_type=node.type,
                    path=next_path,
                    edge_types=next_edges,
                    hops=len(next_path) - 1,
                    owner=node.owner,
                )
            )
            queue.append((from_id, next_path, next_edges))
    return impacts
