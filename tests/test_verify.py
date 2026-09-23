"""Citation verification (PRD R2.4, R2.5). No model.

VF-1..VF-8 of the gold `verifier_cases` section is the fixture, per TDD 5.
"""

import json

import pytest

from strata import db, diff, ingest, verify
from strata.models import Change, Claim

GOLD = json.load(open("data/gold/gold.json", encoding="utf-8"))
CASES = GOLD["verifier_cases"]
CHANGE_IDS = {"CH-7": "c:v1->v2:p12", "CH-15": "c:v2->v3:-p14"}


@pytest.fixture(scope="module")
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    for v in ("v1", "v2", "v3"):
        ingest.ingest_version(c, f"data/proceeding/{v}.md")
    yield c
    c.close()


@pytest.fixture(scope="module")
def changes(conn):
    found = {}
    for a, b in (("v1", "v2"), ("v2", "v3")):
        for change in diff.diff_versions(conn, a, b):
            found[change.change_id] = change
    return found


@pytest.fixture(scope="module")
def paragraphs(conn):
    return {r["para_id"]: r["text"] for r in conn.execute("select para_id, text from paragraphs")}


def claim_for(case, change_id):
    return Claim(
        claim_id=case["id"],
        change_id=change_id,
        material=True,
        version_status="draft",
        obligation_change="modified",
        summary="",
        quote=case["quote"],
        quote_para_id=case["quote_para_id"],
        model_confidence=0.9,
    )


def case_inputs(case, changes, paragraphs):
    """Return (claim, paragraphs, change) for one gold row."""
    change = changes[CHANGE_IDS[case["change"]]]
    texts = dict(paragraphs)
    if "source_text_override" in case:
        override = case["source_text_override"]
        texts[case["quote_para_id"]] = override
        marker = override.index("thirty (30)")
        change = Change(
            change_id=change.change_id,
            from_version=change.from_version,
            to_version=change.to_version,
            para_id_from=change.para_id_from,
            para_id_to=change.para_id_to,
            kind=change.kind,
            spans_from=change.spans_from,
            spans_to=[(marker, marker + len("thirty (30) "))],
            text_from=change.text_from,
            text_to=override,
        )
    return claim_for(case, change.change_id), texts, change


# --- the gold cases -------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_gold_verifier_case(case, changes, paragraphs):
    claim, texts, change = case_inputs(case, changes, paragraphs)
    result = verify.verify_claim(claim, texts, change)

    assert result.status == case["expected_status"], case["tests"]
    if "expected_reason" in case:
        assert result.reason == case["expected_reason"], case["tests"]
    if "expected_distance" in case:
        assert result.edit_distance == case["expected_distance"]
    if "expected_max_distance" in case:
        assert result.edit_distance <= case["expected_max_distance"]
    if result.status in ("verified", "near"):
        assert result.reason is None
        assert result.match_start is not None and result.match_end is not None


def test_vf1_match_points_at_the_quote(changes, paragraphs):
    case = next(c for c in CASES if c["id"] == "VF-1")
    claim, texts, change = case_inputs(case, changes, paragraphs)
    result = verify.verify_claim(claim, texts, change)
    assert texts["v2:p12"][result.match_start : result.match_end] == case["quote"]
    assert result.overlaps_change is True


def test_vf2_near_match_records_the_winning_window(changes, paragraphs):
    """A near match must say which window won, or match_start is meaningless."""
    case = next(c for c in CASES if c["id"] == "VF-2")
    claim, texts, change = case_inputs(case, changes, paragraphs)
    result = verify.verify_claim(claim, texts, change)

    assert result.status == "near"
    assert result.edit_distance == 1
    assert result.match_start > 0, "window offset was not recorded"
    matched = texts["v2:p12"][result.match_start : result.match_end]
    assert matched.startswith("shall complete the interconnection study")
    assert "thirty (30)" in matched
    assert result.overlaps_change is True


def test_vf4_wrong_paragraph_is_decided_before_any_search(changes, paragraphs):
    """VF-3 and VF-4 share a quote; only the named paragraph differs."""
    vf3 = next(c for c in CASES if c["id"] == "VF-3")
    vf4 = next(c for c in CASES if c["id"] == "VF-4")
    assert vf3["quote"] == vf4["quote"]

    claim, texts, change = case_inputs(vf4, changes, paragraphs)
    assert claim.quote in texts["v2:p11"], "the quote really is in p11"
    result = verify.verify_claim(claim, texts, change)
    assert result.reason == "wrong_paragraph"
    assert result.match_start is None, "no search should have run"
    assert result.edit_distance is None


def test_vf5_overlap_is_checked_after_a_successful_find(changes, paragraphs):
    case = next(c for c in CASES if c["id"] == "VF-5")
    claim, texts, change = case_inputs(case, changes, paragraphs)
    result = verify.verify_claim(claim, texts, change)
    assert result.reason == "quote_outside_changed_span"
    assert result.match_start == 0, "the quote was found, at the paragraph opening"
    assert result.overlaps_change is False


def test_vf7_removal_verifies_against_the_from_version(changes, paragraphs):
    case = next(c for c in CASES if c["id"] == "VF-7")
    claim, texts, change = case_inputs(case, changes, paragraphs)
    assert change.kind == "removed"
    assert change.para_id_to is None
    result = verify.verify_claim(claim, texts, change)
    assert result.status == "verified"


def test_vf8_gold_row_actually_exercises_normalization():
    """The row must contain the characters its description claims."""
    case = next(c for c in CASES if c["id"] == "VF-8")
    source = case["source_text_override"]
    assert " " in source, "no non-breaking space"
    assert "“" in source or "’" in source, "no curly quotes"
    assert case["quote"] not in source, "quote matches before normalization; row is vacuous"


def test_vf8_offsets_map_back_through_the_odd_characters(changes, paragraphs):
    case = next(c for c in CASES if c["id"] == "VF-8")
    claim, texts, change = case_inputs(case, changes, paragraphs)
    result = verify.verify_claim(claim, texts, change)

    assert result.status == "verified"
    assert result.edit_distance == 0
    matched = texts["v2:p12"][result.match_start : result.match_end]
    assert matched != case["quote"], "the original still holds the odd characters"
    assert verify.normalize(matched)[0] == case["quote"]


# --- normalization and the offset map -------------------------------------


def test_normalize_substitutions():
    text = "The  utility’s  study — shall begin"
    out, _ = verify.normalize(text)
    assert out == "The utility's study - shall begin"


def test_normalize_offset_map_is_exact():
    """Hand-checked map for a string using every substitution."""
    text = "a  b’c  —d"
    out, offsets = verify.normalize(text)
    assert out == "a b'c -d"
    assert len(offsets) == len(out) + 1
    assert offsets == [0, 1, 3, 4, 5, 6, 8, 9, 10]
    assert offsets[-1] == len(text)
    for i, ch in enumerate(out):
        assert verify.normalize(text[offsets[i]])[0] in (ch, " ")


def test_offset_map_round_trips_on_real_text(paragraphs):
    text = paragraphs["v2:p12"]
    out, offsets = verify.normalize(text)
    for start in range(0, len(out) - 20, 7):
        end = start + 20
        piece = text[offsets[start] : offsets[end]]
        assert verify.normalize(piece)[0].strip() == out[start:end].strip()


def test_normalize_collapses_runs_and_maps_to_the_first_character():
    out, offsets = verify.normalize("a    b")
    assert out == "a b"
    assert offsets[1] == 1, "the emitted space maps to the start of the run"
    assert offsets[2] == 5


def test_normalize_handles_empty_and_whitespace_only():
    assert verify.normalize("")[0] == ""
    assert verify.normalize("   ")[0].strip() == ""


def test_near_threshold_scales_with_quote_length(changes, paragraphs):
    assert verify.threshold("x" * 50) == 3
    assert verify.threshold("x" * 100) == 4
    assert verify.threshold("x" * 250) == 10


def test_paraphrase_is_rejected_not_near(changes, paragraphs):
    """VF-6: the threshold must reject a paraphrase of the same length."""
    case = next(c for c in CASES if c["id"] == "VF-6")
    claim, texts, change = case_inputs(case, changes, paragraphs)
    result = verify.verify_claim(claim, texts, change)
    assert result.status == "rejected"
    assert result.reason == "quote_not_found"


def test_unknown_paragraph_is_wrong_paragraph(changes, paragraphs):
    change = changes["c:v1->v2:p12"]
    claim = Claim(
        claim_id="x", change_id=change.change_id, material=True, version_status="draft",
        obligation_change="modified", summary="", quote="anything",
        quote_para_id="v9:p99", model_confidence=0.5,
    )
    result = verify.verify_claim(claim, paragraphs, change)
    assert result.reason == "wrong_paragraph"


def test_empty_span_side_cannot_overlap(changes, paragraphs):
    """c:v2->v3:p7 is a pure deletion, so the to-side has no changed span."""
    change = changes["c:v2->v3:p7"]
    assert change.spans_to == []
    claim = Claim(
        claim_id="x", change_id=change.change_id, material=True, version_status="final",
        obligation_change="none", summary="", quote="The rule applies to each electric utility",
        quote_para_id="v3:p7", model_confidence=0.5,
    )
    result = verify.verify_claim(claim, paragraphs, change)
    assert result.reason == "quote_outside_changed_span"

    from_side = Claim(
        claim_id="y", change_id=change.change_id, material=True, version_status="final",
        obligation_change="none", summary="", quote="The proposed rule applies",
        quote_para_id="v2:p7", model_confidence=0.5,
    )
    assert verify.verify_claim(from_side, paragraphs, change).status == "verified"
