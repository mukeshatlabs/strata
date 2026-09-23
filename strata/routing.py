"""Escalation rules and review task creation (TDD 3.6, 3.8).

Two jobs. `route` decides whether a claim can go to the owner of the work it
affects or must go to an expert first; the rules are applied in a fixed order so
the reason a reviewer sees is the first thing that was wrong, not the last.
`create_tasks` turns impacts into work, one task per reached node.
"""

from .models import ReviewTask

THRESHOLD = 0.7

ACTIONS = {
    ("modified", "obligation"): "Update the obligation text to match the changed requirement",
    ("removed", "obligation"): "Retire the obligation and record the version that removed it",
    ("created", "obligation"): "Create the obligation node, assign an owner, and link it to affected projects and documents",
    ("modified", "project"): "Review the project plan and schedule against the changed requirement",
    ("removed", "project"): "Review whether the project is still required",
    ("modified", "document"): "Update the document to reflect the changed requirement",
    ("removed", "document"): "Review whether the document still applies and remove the requirement",
}

ESCALATED_ACTION = "Resolve in expert review before this reaches an owner"


def route(claim, verification, links) -> tuple[str, str | None]:
    """Return (queue, reason) for one claim. Rules are applied in order (TDD 3.6).

    1. a rejected citation, reported with the verifier's own reason
    2. a link whose rationale quote did not verify
    3. a newly created duty, which no existing node can be the link for
    4. a link below the confidence threshold
    5. a modified or removed duty with no confident link at all
    6. otherwise the owner queue
    """
    if verification is not None and verification.status == "rejected":
        return "expert", verification.reason

    if any(link.rationale_status == "rejected" for link in links):
        return "expert", "rationale_unverified"

    if claim.obligation_change == "created":
        return "expert", "new_obligation"

    if any(link.confidence < THRESHOLD for link in links):
        return "expert", "low_confidence"

    if claim.obligation_change in ("modified", "removed") and not any(
        link.confidence >= THRESHOLD for link in links
    ):
        return "expert", "no_confident_link"

    return "owner", None


def action_for(obligation_change: str, node_type: str) -> str:
    """Look up the recommended action, falling back to a readable default."""
    return ACTIONS.get(
        (obligation_change, node_type),
        f"Review the {node_type} against the changed requirement",
    )


def create_tasks(claim, verification, links, impacts, graph) -> list[ReviewTask]:
    """Create review tasks for one claim (TDD 3.8).

    An escalated claim produces a single expert task with no node: nothing
    reaches an owner until a human has agreed the claim is sound. Otherwise one
    task per node, the obligations the claim links to plus everything
    propagation reached from them. A node reached by several routes is one task
    carrying all of them.
    """
    queue, reason = route(claim, verification, links)
    if queue == "expert":
        return [
            ReviewTask(
                task_id=f"{claim.claim_id}:expert",
                project_id="proj-1",
                claim_id=claim.claim_id,
                change_id=claim.change_id,
                node_id=None,
                queue="expert",
                assignee=None,
                recommended_action=ACTIONS.get(
                    (claim.obligation_change, "obligation"), ESCALATED_ACTION
                ),
                reason=reason,
            )
        ]

    # The obligations themselves, which propagation does not return, then
    # everything reached from them. Insertion order gives a stable task order.
    reached: dict[str, dict] = {}
    for link in links:
        node = graph.nodes.get(link.obligation_id)
        if node is None:
            continue
        reached[node.node_id] = {
            "node": node,
            "paths": [[node.node_id]],
            "obligations": [node.node_id],
        }
    for impact in impacts:
        node = graph.nodes.get(impact.node_id)
        if node is None:
            continue
        entry = reached.setdefault(
            impact.node_id, {"node": node, "paths": [], "obligations": []}
        )
        if impact.path not in entry["paths"]:
            entry["paths"].append(impact.path)
        if impact.obligation_id not in entry["obligations"]:
            entry["obligations"].append(impact.obligation_id)

    return [
        ReviewTask(
            task_id=f"{claim.claim_id}:{node_id}",
            project_id="proj-1",
            claim_id=claim.claim_id,
            change_id=claim.change_id,
            node_id=node_id,
            queue="owner",
            assignee=entry["node"].owner,
            recommended_action=action_for(claim.obligation_change, entry["node"].type),
            reason=None,
            paths=entry["paths"],
            obligation_ids=entry["obligations"],
        )
        for node_id, entry in reached.items()
    ]
