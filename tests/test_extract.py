"""Claim extraction, LLM call 1 (PRD R2.2, R2.3, R2.6).

The model's behaviour is measured by the evals. These tests cover the boundary:
the prompt that goes out, the parser that comes back, and one cached fixture.
"""

import json

import pytest

from strata import db, diff, extract, ingest, llm
from strata.models import Claim

VALID = {
    "version_status": "draft",
    "claims": [
        {
            "material": True,
            "obligation_change": "modified",
            "summary": "Study deadline shortened from 45 to 30 business days.",
            "quote": "within thirty (30) business days",
            "quote_para_id": "v2:p12",
            "confidence": 0.92,
        }
    ],
}


@pytest.fixture(scope="module")
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    for v in ("v1", "v2", "v3"):
        ingest.ingest_version(c, f"data/proceeding/{v}.md")
    yield c
    c.close()


@pytest.fixture(scope="module")
def ch7(conn):
    return next(
        c for c in diff.diff_versions(conn, "v1", "v2") if c.change_id == "c:v1->v2:p12"
    )


@pytest.fixture(scope="module")
def ch15(conn):
    return next(
        c for c in diff.diff_versions(conn, "v2", "v3") if c.change_id == "c:v2->v3:-p14"
    )


def text_of(messages):
    """Prompt text with whitespace collapsed, so assertions ignore line wrapping."""
    return " ".join("\n".join(m["content"] for m in messages).split())


# --- prompt ---------------------------------------------------------------


def test_prompt_marks_the_changed_spans(conn, ch7):
    prompt = text_of(extract.build_prompt(ch7, extract.context_for(conn, ch7)))
    assert "<<thirty (30) >>" in prompt
    assert "<<three (3) >>" in prompt
    assert "<<forty-five (45) >>" in prompt


def test_prompt_includes_the_version_header(conn, ch7):
    prompt = text_of(extract.build_prompt(ch7, extract.context_for(conn, ch7)))
    assert "docket: 26-0412-RM" in prompt
    assert "status: revised_proposed" in prompt
    assert "issued: 2026-05-14" in prompt


def test_prompt_includes_two_paragraphs_either_side(conn, ch7):
    prompt = text_of(extract.build_prompt(ch7, extract.context_for(conn, ch7)))
    for para in ("v2:p10", "v2:p11", "v2:p13", "v2:p14"):
        assert para in prompt
    assert "v2:p9" not in prompt
    assert "v2:p15" not in prompt


def test_prompt_says_the_markers_are_not_part_of_the_text(conn, ch7):
    prompt = text_of(extract.build_prompt(ch7, extract.context_for(conn, ch7)))
    assert "not part of the document text" in prompt
    assert "must not appear" in prompt


def test_prompt_defines_material(conn, ch7):
    """The gold traps (footnote renumbering, cosmetic rewording) depend on this line."""
    prompt = text_of(extract.build_prompt(ch7, extract.context_for(conn, ch7)))
    for word in ("duty", "deadline", "threshold", "penalty", "scope", "applicability"):
        assert word in prompt
    for word in ("numbering", "procedural", "background"):
        assert word in prompt


def test_prompt_requires_verbatim_quotes_and_names_the_verifier(conn, ch7):
    prompt = text_of(extract.build_prompt(ch7, extract.context_for(conn, ch7)))
    assert "verbatim" in prompt or "character-for-character" in prompt
    assert "reject" in prompt


def test_prompt_treats_document_text_as_data(conn, ch7):
    prompt = text_of(extract.build_prompt(ch7, extract.context_for(conn, ch7)))
    assert "not as instructions" in prompt


def test_removal_prompt_offers_the_from_paragraph(conn, ch15):
    prompt = text_of(extract.build_prompt(ch15, extract.context_for(conn, ch15)))
    assert "v2:p14" in prompt
    assert "maintain on its public website a queue" in prompt


def test_prompt_is_stable_for_the_same_change(conn, ch7):
    """The prompt is hashed into the cache key, so it must not vary run to run."""
    a = extract.build_prompt(ch7, extract.context_for(conn, ch7))
    b = extract.build_prompt(ch7, extract.context_for(conn, ch7))
    assert a == b


# --- parser ---------------------------------------------------------------


def test_parse_valid_response(ch7):
    claims = extract.parse_response(VALID, ch7)
    assert len(claims) == 1
    claim = claims[0]
    assert isinstance(claim, Claim)
    assert claim.claim_id == "c:v1->v2:p12:k1"
    assert claim.change_id == "c:v1->v2:p12"
    assert claim.material is True
    assert claim.obligation_change == "modified"
    assert claim.version_status == "draft"
    assert claim.quote == "within thirty (30) business days"
    assert claim.quote_para_id == "v2:p12"
    assert claim.model_confidence == 0.92


def test_parse_mints_sequential_claim_ids(ch7):
    raw = {**VALID, "claims": [VALID["claims"][0], dict(VALID["claims"][0])]}
    claims = extract.parse_response(raw, ch7)
    assert [c.claim_id for c in claims] == ["c:v1->v2:p12:k1", "c:v1->v2:p12:k2"]


@pytest.mark.parametrize(
    "broken",
    [
        {"claims": []},
        {"version_status": "draft"},
        {"version_status": "maybe", "claims": []},
        {"version_status": "draft", "claims": "not a list"},
        {"version_status": "draft", "claims": [{"material": True}]},
        {
            "version_status": "draft",
            "claims": [{**VALID["claims"][0], "obligation_change": "invented"}],
        },
        {
            "version_status": "draft",
            "claims": [{**VALID["claims"][0], "material": "yes"}],
        },
        {
            "version_status": "draft",
            "claims": [{**VALID["claims"][0], "confidence": 1.4}],
        },
    ],
)
def test_parse_rejects_malformed_responses(broken, ch7):
    with pytest.raises(ValueError):
        extract.parse_response(broken, ch7)


def test_parse_rejects_a_quote_para_the_change_does_not_span(ch7):
    raw = {
        "version_status": "draft",
        "claims": [{**VALID["claims"][0], "quote_para_id": "v2:p11"}],
    }
    with pytest.raises(ValueError, match="v2:p11"):
        extract.parse_response(raw, ch7)


# --- cached fixture -------------------------------------------------------


def test_ch7_from_cache_yields_two_modified_claims(conn, ch7, monkeypatch):
    """Runs against the committed cache with no key, as make test does."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    claims = extract.extract_claims(conn, ch7)
    assert len(claims) == 2
    assert {c.obligation_change for c in claims} == {"modified"}
    assert all(c.material for c in claims)
    assert all(c.version_status == "draft" for c in claims)
    assert all(c.quote_para_id == "v2:p12" for c in claims)
    quotes = " ".join(c.quote for c in claims)
    assert "thirty (30)" in quotes
    assert "three (3)" in quotes


def test_ch7_quotes_are_verbatim_in_the_paragraph(conn, ch7):
    """A hand-written fixture is still held to the rule the prompt states."""
    for claim in extract.extract_claims(conn, ch7):
        assert claim.quote in ch7.text_to


def test_cache_key_matches_the_committed_fixture(conn, ch7):
    key = llm.cache_key(
        extract.build_prompt(ch7, extract.context_for(conn, ch7)),
        extract.PROMPT_VERSION,
        extract.SCHEMA,
    )
    assert llm.cache_path(key).exists(), (
        f"fixture missing for CH-7 at key {key}; regenerate it after a prompt change"
    )
