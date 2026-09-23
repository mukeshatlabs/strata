"""Run the pipeline against the gold set and write evals/results.md (TDD 4).

Offline: every model response comes from data/llm_cache. Metrics are the ones
listed in TDD Section 4, each reported with the gold rows that fed it.
"""

import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from strata import (  # noqa: E402
    db, diff, events, extract, graph, ingest, mapping, pipeline, routing, verify,
)
from strata.models import Claim, Event, Link, Verification  # noqa: E402

GOLD = json.load(open("data/gold/gold.json", encoding="utf-8"))
PAIRS = (("v1", "v2"), ("v2", "v3"))
RESULTS = pathlib.Path("evals/results.md")
THRESHOLDS = (0.5, 0.7, 0.9)
PROJECT = "proj-1"


def build():
    """Ingest into a fresh database. Nothing is replayed yet."""
    conn = db.connect(":memory:")
    db.init_schema(conn)
    for version in ("v1", "v2", "v3"):
        ingest.ingest_version(conn, f"data/proceeding/{version}.md")
    ingest.ingest_company(conn, "data/company/meridian.json")
    return conn


def gold_change_ids(conn):
    """Map each gold change row to the change_id the differ produced."""
    found = {}
    for pair, (a, b) in zip(("v1->v2", "v2->v3"), PAIRS):
        for change in diff.diff_versions(conn, a, b):
            found[(pair, change.para_id_from, change.para_id_to)] = change
    mapped = {}
    for row in GOLD["changes"]:
        change = found.get((row["pair"], row["from_para"], row["to_para"]))
        if change is not None:
            mapped[row["id"]] = change
    return mapped


def collect(conn):
    """Run both version pairs through the real pipeline and read the log back.

    The pipeline is the thing under test, so the eval drives it rather than
    re-implementing the sequence. OBL-12 is created between the two runs exactly
    as `make live` did, which is also what makes the v2 to v3 mapping prompts
    match their cached responses: the obligation list differs between the pairs.
    """
    for version in ("v2", "v3"):
        run = pipeline.run_version(conn, PROJECT, version)
        if version == "v2":
            pipeline.create_obl_12(conn, PROJECT, run["tasks"])

    changes = {}
    for pair, (a, b) in zip(("v1->v2", "v2->v3"), PAIRS):
        for change in diff.diff_versions(conn, a, b):
            changes[change.change_id] = (pair, change)

    by_claim = {}
    for event in events.history(conn, PROJECT):
        body = event.payload
        if event.type == "claim_extracted":
            pair, change = changes[body["change_id"]]
            by_claim[body["claim_id"]] = {
                "pair": pair, "change": change,
                "claim": Claim(**body), "verification": None, "links": [],
            }
        elif event.type == "claim_verified":
            by_claim[body["claim_id"]]["verification"] = Verification(**body)
        elif event.type == "link_proposed":
            by_claim[body["claim_id"]]["links"].append(Link(**body))
    return list(by_claim.values())


def pct(numerator, denominator):
    return "n/a" if not denominator else f"{numerator / denominator * 100:.0f}%"


def score(records, by_gold):
    """Return every metric as a list of (name, value, detail) rows."""
    rows = []
    claims_by_change = collections.defaultdict(list)
    for record in records:
        claims_by_change[record["change"].change_id].append(record)

    # Materiality, per gold change row.
    judged = [r for r in GOLD["changes"] if r["kind"] != "unchanged" and r["id"] in by_gold]
    hits, misses = 0, []
    for row in judged:
        predicted = any(r["claim"].material for r in claims_by_change[by_gold[row["id"]].change_id])
        if predicted == row["material"]:
            hits += 1
        else:
            misses.append(f"{row['id']} expected {row['material']}, got {predicted}")
    rows.append(("Materiality accuracy", f"{hits}/{len(judged)} ({pct(hits, len(judged))})",
                 "; ".join(misses) or "no misses"))

    # Draft/final, a majority vote per version with the split reported.
    status_rows = {r["version"]: r for r in GOLD["version_status"]}
    correct, detail = 0, []
    for pair, version in (("v1->v2", "v2"), ("v2->v3", "v3")):
        votes = collections.Counter(
            r["claim"].version_status for r in records if r["pair"] == pair
        )
        if not votes:
            continue
        winner = votes.most_common(1)[0][0]
        expected = status_rows[version]["expected"]
        correct += winner == expected
        split = ", ".join(f"{k} {v}" for k, v in sorted(votes.items()))
        detail.append(f"{version}: {winner} (expected {expected}; split {split})")
    rows.append(("Draft/final accuracy", f"{correct}/2 ({pct(correct, 2)})", "; ".join(detail)))

    # Extraction precision and recall on (change_id, obligation_change).
    expected_pairs = {
        (by_gold[r["id"]].change_id, r["obligation_change"])
        for r in GOLD["changes"]
        if r["id"] in by_gold and r.get("obligation_change")
    }
    # Only changes gold actually judges; gold covers 16 of 27 changes, so
    # counting claims from the rest as false positives measures nothing.
    scored_changes = {c.change_id for c in by_gold.values()}
    predicted_pairs = {
        (r["change"].change_id, r["claim"].obligation_change)
        for r in records
        if r["change"].change_id in scored_changes
    }
    tp = len(expected_pairs & predicted_pairs)
    rows.append(("Extraction precision", pct(tp, len(predicted_pairs)),
                 f"{tp} of {len(predicted_pairs)} predicted pairs on gold-judged"
                 f" changes are in gold"))
    rows.append(("Extraction recall", pct(tp, len(expected_pairs)),
                 f"{tp} of {len(expected_pairs)} gold pairs found"))

    # Citation fidelity.
    statuses = collections.Counter(r["verification"].status for r in records)
    reasons = collections.Counter(
        r["verification"].reason for r in records if r["verification"].reason
    )
    total = sum(statuses.values())
    rows.append(("Citation fidelity",
                 f"{statuses['verified']} verified, {statuses['near']} near,"
                 f" {statuses['rejected']} rejected",
                 f"rejection rate {pct(statuses['rejected'], total)}"
                 + (f"; reasons {dict(reasons)}" if reasons else "")))

    # Mapping precision and recall on (change_id, obligation_id).
    expected_links, predicted_links = set(), set()
    for row in GOLD["changes"]:
        if row["id"] in by_gold:
            for obligation in row.get("expected_links", []):
                expected_links.add((by_gold[row["id"]].change_id, obligation))
    acceptable = set()
    for row in GOLD["changes"]:
        if row["id"] in by_gold:
            for obligation in row.get("acceptable_extra_links", []):
                acceptable.add((by_gold[row["id"]].change_id, obligation))
    for record in records:
        for link in record["links"]:
            if link.confidence >= routing.THRESHOLD:
                predicted_links.add((record["change"].change_id, link.obligation_id))
    tp = len(expected_links & predicted_links)
    spurious = predicted_links - expected_links - acceptable
    rows.append(("Mapping precision", pct(tp + len(predicted_links & acceptable),
                                          len(predicted_links)),
                 f"{len(spurious)} link(s) outside gold and its allowances"))
    rows.append(("Mapping recall", pct(tp, len(expected_links)),
                 f"{tp} of {len(expected_links)} gold links found"))

    return rows


def impact_rows(company):
    """Impact coverage, including the two-hop cases."""
    found, expected, misses = 0, 0, []
    for row in GOLD["impacts"]:
        reached = {i.node_id: i for i in graph.propagate(company, row["from_obligation"])}
        for want in row["expected"]:
            expected += 1
            impact = reached.get(want["node"])
            if impact and impact.path == want["path"]:
                found += 1
            else:
                misses.append(f"{row['id']}/{want['node']}")
    leaks = []
    for row in GOLD["not_impacted"]:
        obligation = {"CH-7": "OBL-3", "CH-15": "OBL-6"}[row["change"]]
        reached = {i.node_id for i in graph.propagate(company, obligation)}
        leaks += sorted(reached & set(row["nodes"]))
    two_hop = sum(1 for r in GOLD["impacts"] for e in r["expected"] if e["hops"] == 2)
    return [
        ("Impact coverage", f"{found}/{expected} ({pct(found, expected)})",
         (f"misses {misses}" if misses else "no misses")
         + f"; {two_hop} two-hop impacts in gold"),
        ("Impact leakage", f"{len(leaks)}", "nodes reached that gold excludes"
         + (f": {leaks}" if leaks else "")),
    ]


def escalation(records, company):
    """Queue counts at each threshold, so the trade-off is visible (TDD 3.6)."""
    table = []
    confidences = sorted(l.confidence for r in records for l in r["links"])
    original = routing.THRESHOLD
    for threshold in THRESHOLDS:
        routing.THRESHOLD = threshold
        queues = collections.Counter()
        for record in records:
            queue, reason = routing.route(
                record["claim"], record["verification"], record["links"]
            )
            queues[queue] += 1
        table.append((threshold, queues["owner"], queues["expert"]))
    routing.THRESHOLD = original
    return table, confidences


def render(rows, impacts, escalation_result, records):
    escalations, confidences = escalation_result
    """Write the markdown report."""
    out = ["# Eval results", "",
           f"Prompt versions: extract `{extract.PROMPT_VERSION}`,"
           f" mapping `{mapping.PROMPT_VERSION}`. Offline from `data/llm_cache`.", "",
           f"{len(records)} claims over"
           f" {len({r['change'].change_id for r in records})} changes.", "",
           "| Metric | Value | Detail |", "| --- | --- | --- |"]
    for name, value, detail in rows + impacts:
        out.append(f"| {name} | {value} | {detail} |")
    out += ["", "## Escalation at each threshold", "",
            "| Threshold | Owner queue | Expert queue |", "| --- | --- | --- |"]
    for threshold, owner, expert in escalations:
        out.append(f"| {threshold} | {owner} | {expert} |")
    if confidences:
        flat = len({(o, e) for _, o, e in escalations}) == 1
        out += ["", f"Link confidences range {min(confidences):.2f} to"
                    f" {max(confidences):.2f} over {len(confidences)} links."
                    + (" The counts are identical at every threshold because no link"
                       " falls below the lowest one: the escalations counted here come"
                       " from the other rules, not from confidence." if flat else "")]
    out.append("")
    RESULTS.write_text("\n".join(out), encoding="utf-8")
    return "\n".join(out)


def main() -> int:
    conn = build()
    by_gold = gold_change_ids(conn)
    records = collect(conn)
    company = graph.load(conn, events.effective(conn, PROJECT))
    report = render(
        score(records, by_gold), impact_rows(company), escalation(records, company), records
    )
    print(report)
    print(f"\nwritten to {RESULTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
