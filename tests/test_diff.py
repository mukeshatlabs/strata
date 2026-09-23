"""Version diffing (PRD R2.1). Deterministic, no model. Driven by gold `changes`."""

import json

import pytest

from strata import db, diff, ingest

GOLD = json.load(open("data/gold/gold.json", encoding="utf-8"))
CHANGES = GOLD["changes"]


@pytest.fixture(scope="module")
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    for v in ("v1", "v2", "v3"):
        ingest.ingest_version(c, f"data/proceeding/{v}.md")
    ingest.ingest_company(c, "data/company/meridian.json")
    yield c
    c.close()


@pytest.fixture(scope="module")
def changes(conn):
    return {
        "v1->v2": diff.diff_versions(conn, "v1", "v2"),
        "v2->v3": diff.diff_versions(conn, "v2", "v3"),
    }


def gold_rows(kind=None):
    return [r for r in CHANGES if kind is None or r["kind"] == kind]


@pytest.mark.parametrize("row", gold_rows(), ids=[r["id"] for r in CHANGES])
def test_gold_change_is_emitted_with_the_right_kind_and_paragraphs(row, changes):
    """Every gold row: emitted with matching kind and paragraphs, or absent if unchanged."""
    found = [
        c
        for c in changes[row["pair"]]
        if c.para_id_from == row["from_para"] and c.para_id_to == row["to_para"]
    ]
    if row["kind"] == "unchanged":
        assert found == [], f"{row['id']}: unchanged paragraph must emit no record"
        return
    assert len(found) == 1, f"{row['id']}: expected exactly one change, got {len(found)}"
    assert found[0].kind == row["kind"], row["id"]


def test_unchanged_paragraphs_emit_nothing(changes):
    """CH-9, CH-14, CH-18: renumbered or identical text, no record on either side."""
    for pair, para in (("v1->v2", "v1:p17"), ("v2->v3", "v2:p12"), ("v2->v3", "v2:p18")):
        assert not [c for c in changes[pair] if c.para_id_from == para]


def test_renumbering_alignment_v1_to_v2(changes):
    """CH-10: v1:p21 aligns to v2:p22 after the insertion at v2:p17, not to v2:p21."""
    c = next(c for c in changes["v1->v2"] if c.para_id_from == "v1:p21")
    assert c.para_id_to == "v2:p22"
    assert c.kind == "modified"


def test_renumbering_alignment_v2_to_v3(changes):
    """CH-16: v2:p15 aligns to v3:p14 after v2:p14 was removed."""
    c = next(c for c in changes["v2->v3"] if c.para_id_from == "v2:p15")
    assert c.para_id_to == "v3:p14"
    assert c.kind == "modified"


def test_addition_has_no_from_paragraph(changes):
    """CH-8: the new penalty paragraph is added at v2:p17."""
    added = [c for c in changes["v1->v2"] if c.kind == "added"]
    assert [c.para_id_to for c in added] == ["v2:p17"]
    assert added[0].para_id_from is None
    assert added[0].text_from == ""
    assert "penalty" in added[0].text_to.lower() or "$" in added[0].text_to


def test_removal_has_no_to_paragraph(changes):
    """CH-15: the public queue obligation is removed from v2:p14."""
    c = next(c for c in changes["v2->v3"] if c.para_id_from == "v2:p14")
    assert c.kind == "removed"
    assert c.para_id_to is None
    assert c.text_to == ""
    assert "maintain on its public website a queue" in c.text_from


def test_unequal_block_pairs_by_similarity_not_position(changes):
    """[v2:p14, v2:p15] -> [v3:p14]: p15 pairs (0.80), p14 drops out (0.50)."""
    by_from = {c.para_id_from: c for c in changes["v2->v3"]}
    assert by_from["v2:p14"].kind == "removed"
    assert by_from["v2:p15"].para_id_to == "v3:p14"

    by_from = {c.para_id_from: c for c in changes["v1->v2"]}
    assert by_from["v1:p25"].kind == "removed"
    assert by_from["v1:p26"].para_id_to == "v2:p26"


def test_low_similarity_positional_pairs_are_still_modified(changes):
    """CH-17 (0.79) and CH-19 (0.22): equal-length blocks pair without a threshold."""
    by_from = {c.para_id_from: c for c in changes["v2->v3"]}
    assert by_from["v2:p17"].para_id_to == "v3:p16"
    assert by_from["v2:p17"].kind == "modified"
    assert by_from["v2:p21"].para_id_to == "v3:p20"
    assert by_from["v2:p21"].kind == "modified"


def test_ch7_spans_cover_both_changed_numbers(changes):
    """CH-7: one paragraph, two obligations. Spans must isolate both deadlines."""
    c = next(c for c in changes["v1->v2"] if c.para_id_from == "v1:p12")
    covered_to = [c.text_to[s:e] for s, e in c.spans_to]
    covered_from = [c.text_from[s:e] for s, e in c.spans_from]
    assert any("thirty (30)" in t for t in covered_to)
    assert any("three (3)" in t for t in covered_to)
    assert any("forty-five (45)" in t for t in covered_from)
    assert any("five (5)" in t for t in covered_from)


def test_spans_are_within_bounds_and_ordered(changes):
    for pair in changes:
        for c in changes[pair]:
            for spans, text in ((c.spans_from, c.text_from), (c.spans_to, c.text_to)):
                last = 0
                for start, end in spans:
                    assert 0 <= start < end <= len(text), c.change_id
                    assert start >= last, f"{c.change_id}: spans out of order"
                    last = end


def test_change_ids_are_unique_and_encode_kind(changes):
    """The v2->v3 block would collide on p14 if removals were not marked."""
    for pair, records in changes.items():
        ids = [c.change_id for c in records]
        assert len(ids) == len(set(ids)), f"{pair}: duplicate change_id"
    by_from = {c.para_id_from: c for c in changes["v2->v3"]}
    assert by_from["v2:p14"].change_id == "c:v2->v3:-p14"
    assert by_from["v2:p15"].change_id == "c:v2->v3:p15"
    added = next(c for c in changes["v1->v2"] if c.kind == "added")
    assert added.change_id == "c:v1->v2:+p17"


def test_modified_changes_have_spans_and_differing_text(changes):
    """A modification changes text on at least one side.

    Only one side is required: a pure word insertion leaves spans_from empty
    (c:v1->v2:p1 adds "REVISED") and a pure deletion leaves spans_to empty
    (c:v2->v3:p7 drops "proposed").
    """
    for pair in changes:
        for c in changes[pair]:
            if c.kind == "modified":
                assert c.text_from != c.text_to, c.change_id
                assert c.spans_from or c.spans_to, c.change_id


def test_pure_insertion_and_deletion_span_only_the_changed_side(changes):
    insertion = next(c for c in changes["v1->v2"] if c.change_id == "c:v1->v2:p1")
    assert insertion.spans_from == []
    assert [insertion.text_to[s:e] for s, e in insertion.spans_to] == ["REVISED "]

    deletion = next(c for c in changes["v2->v3"] if c.change_id == "c:v2->v3:p7")
    assert deletion.spans_to == []
    assert [deletion.text_from[s:e] for s, e in deletion.spans_from] == ["proposed "]


def test_similarity_uses_autojunk_false(conn):
    """autojunk drops the CH-16 pairing from 0.80 to 0.35 and breaks alignment."""
    import difflib

    texts = {
        r["para_id"]: r["text"]
        for r in conn.execute("select para_id, text from paragraphs")
    }
    a, b = texts["v2:p15"], texts["v3:p14"]
    with_junk = difflib.SequenceMatcher(None, a, b).ratio()
    assert with_junk < diff.SIMILARITY_THRESHOLD < diff._ratio(a, b)


def test_pinned_ratio_for_ch16(conn):
    """CH-16's pairing is the tightest in the data; pin it so a regression is loud."""
    texts = {
        r["para_id"]: r["text"]
        for r in conn.execute("select para_id, text from paragraphs")
    }
    r = diff._ratio(texts["v2:p15"], texts["v3:p14"])
    assert 0.79 < r < 0.81, f"CH-16 pairing ratio moved to {r:.4f}"
    assert r > diff.SIMILARITY_THRESHOLD
    assert diff._ratio(texts["v2:p14"], texts["v3:p14"]) < diff.SIMILARITY_THRESHOLD


def test_versions_diff_to_nothing_against_themselves(conn):
    assert diff.diff_versions(conn, "v2", "v2") == []
