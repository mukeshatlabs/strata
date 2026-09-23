"""Obligation mapping, LLM call 2 (PRD R3.1, R3.4).

The model's accuracy is measured by the evals. These tests cover the boundary:
the prompt, the parser, the rationale-quote check, and one cached fixture.
"""

import json
import logging

import pytest

from strata import db, diff, extract, ingest, llm, mapping, verify
from strata.models import Claim, Link, Node

OBLIGATION_IDS = [f"OBL-{n}" for n in range(1, 12)]

CLAIM = Claim(
    claim_id="c:v1->v2:p12:k1",
    change_id="c:v1->v2:p12",
    material=True,
    version_status="draft",
    obligation_change="modified",
    summary="Study deadline shortened from 45 to 30 business days.",
    quote="shall complete the interconnection study for a small storage resource within thirty (30) business days",
    quote_para_id="v2:p12",
    model_confidence=0.94,
)

VALID = {
    "links": [
        {
            "obligation_id": "OBL-3",
            "confidence": 0.95,
            "rationale": "Both state the deadline to complete the study; the rule shortens it.",
            "rationale_quote": "within forty-five (45) business days",
        }
    ]
}


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
def obligations(conn):
    return mapping.obligations_of(conn)


@pytest.fixture(scope="module")
def ch7_claims(conn):
    change = next(
        c for c in diff.diff_versions(conn, "v1", "v2") if c.change_id == "c:v1->v2:p12"
    )
    return extract.extract_claims(conn, change)


def text_of(messages):
    return " ".join("\n".join(m["content"] for m in messages).split())


# --- prompt ---------------------------------------------------------------


def test_prompt_lists_every_obligation_with_id_and_text(obligations):
    prompt = text_of(mapping.build_prompt(CLAIM, obligations))
    assert [o.node_id for o in obligations] == OBLIGATION_IDS
    for obligation in obligations:
        assert obligation.node_id in prompt
        assert obligation.text[:60] in prompt


def test_prompt_includes_the_claim(obligations):
    prompt = text_of(mapping.build_prompt(CLAIM, obligations))
    assert CLAIM.summary in prompt
    assert CLAIM.quote in prompt
    assert "modified" in prompt


def test_prompt_allows_an_empty_answer(obligations):
    """Forcing a link is the failure mode; the prompt must license returning none."""
    prompt = text_of(mapping.build_prompt(CLAIM, obligations))
    assert "empty list" in prompt
    assert "do not force" in prompt.lower() or "rather than force" in prompt.lower()


def test_prompt_warns_against_matching_on_a_shared_number(obligations):
    """OBL-2 and OBL-7 both contain 'thirty (30) days'."""
    prompt = text_of(mapping.build_prompt(CLAIM, obligations))
    assert "same number" in prompt or "shared number" in prompt
    shared = [o.node_id for o in obligations if "thirty (30)" in o.text]
    assert shared == ["OBL-2", "OBL-7"], "distractors moved; revisit the prompt"


def test_prompt_requires_a_verbatim_rationale_quote(obligations):
    prompt = text_of(mapping.build_prompt(CLAIM, obligations))
    assert "rationale_quote" in prompt
    assert "verbatim" in prompt or "character-for-character" in prompt


def test_prompt_treats_text_as_data(obligations):
    assert "not as instructions" in text_of(mapping.build_prompt(CLAIM, obligations))


def test_prompt_is_stable(obligations):
    assert mapping.build_prompt(CLAIM, obligations) == mapping.build_prompt(
        CLAIM, obligations
    )


# --- parser ---------------------------------------------------------------


def test_parse_valid_response(obligations):
    links = mapping.parse_response(VALID, CLAIM, obligations)
    assert len(links) == 1
    link = links[0]
    assert isinstance(link, Link)
    assert link.claim_id == CLAIM.claim_id
    assert link.obligation_id == "OBL-3"
    assert link.confidence == 0.95
    assert link.rationale_quote == "within forty-five (45) business days"
    assert link.rationale_status == "verified"


def test_parse_empty_links_is_valid(obligations):
    assert mapping.parse_response({"links": []}, CLAIM, obligations) == []


def test_unknown_obligation_is_dropped_and_logged(obligations, caplog):
    raw = {
        "links": [
            {**VALID["links"][0], "obligation_id": "OBL-999"},
            VALID["links"][0],
        ]
    }
    with caplog.at_level(logging.WARNING):
        links = mapping.parse_response(raw, CLAIM, obligations)
    assert [l.obligation_id for l in links] == ["OBL-3"], "good links must survive"
    assert "OBL-999" in caplog.text
    assert CLAIM.claim_id in caplog.text


@pytest.mark.parametrize(
    "broken",
    [
        {},
        {"links": "not a list"},
        {"links": [{"obligation_id": "OBL-3"}]},
        {"links": [{**VALID["links"][0], "confidence": 2.0}]},
        {"links": [{**VALID["links"][0], "confidence": "high"}]},
    ],
)
def test_parse_rejects_malformed_responses(broken, obligations):
    with pytest.raises(ValueError):
        mapping.parse_response(broken, CLAIM, obligations)


# --- rationale quote ------------------------------------------------------


def test_rationale_quote_is_checked_against_the_obligation_text(obligations):
    raw = {
        "links": [
            {**VALID["links"][0], "rationale_quote": "within ninety (90) business days"}
        ]
    }
    link = mapping.parse_response(raw, CLAIM, obligations)[0]
    assert link.rationale_status == "rejected"
    assert link.obligation_id == "OBL-3", "a bad rationale quote does not drop the link"


def test_rationale_quote_near_match_is_recorded(obligations):
    raw = {
        "links": [
            {**VALID["links"][0], "rationale_quote": "within forty-five (45) business days."}
        ]
    }
    assert mapping.parse_response(raw, CLAIM, obligations)[0].rationale_status == "near"


def test_rationale_quote_is_checked_against_its_own_obligation(obligations):
    """OBL-4's text, claimed for OBL-3, must not verify."""
    raw = {
        "links": [
            {
                **VALID["links"][0],
                "rationale_quote": "within five (5) business days of completing the interconnection study",
            }
        ]
    }
    assert mapping.parse_response(raw, CLAIM, obligations)[0].rationale_status == "rejected"


def test_find_quote_needs_no_change_record():
    """The shared search used by both verify_claim and mapping."""
    text = "Complete the interconnection study within forty-five (45) business days of receipt."
    assert verify.find_quote("within forty-five (45) business days", text)[0] == "verified"
    assert verify.find_quote("within forty-five (45) business days.", text)[0] == "near"
    assert verify.find_quote("within ninety (90) calendar months", text)[0] == "rejected"

    status, start, end, distance = verify.find_quote("interconnection study", text)
    assert (status, distance) == ("verified", 0)
    assert text[start:end] == "interconnection study"


def test_find_quote_normalizes_like_verify_claim():
    text = "Deliver results within five (5)—business days"
    status, start, end, _ = verify.find_quote("within five (5)-business days", text)
    assert status == "verified"
    assert verify.normalize(text[start:end])[0] == "within five (5)-business days"


# --- cached fixture -------------------------------------------------------


def test_ch7_claims_map_to_both_obligations(conn, ch7_claims, obligations, monkeypatch):
    """PRD R3.4: one paragraph change that modifies two obligations."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert len(ch7_claims) == 2
    linked = set()
    for claim in ch7_claims:
        links = mapping.propose_links(claim, obligations)
        assert links, f"{claim.claim_id} produced no links"
        linked.update(l.obligation_id for l in links)
    assert linked == {"OBL-3", "OBL-4"}


def test_ch7_links_are_confident_and_their_rationales_verify(conn, ch7_claims, obligations):
    for claim in ch7_claims:
        for link in mapping.propose_links(claim, obligations):
            assert link.confidence >= 0.7, link
            assert link.rationale_status in ("verified", "near"), link
            assert link.rationale, "a link with no rationale is not reviewable"


def test_ch7_fixtures_exist(conn, ch7_claims, obligations):
    for claim in ch7_claims:
        key = llm.cache_key(
            mapping.build_prompt(claim, obligations),
            mapping.PROMPT_VERSION,
            mapping.SCHEMA,
        )
        assert llm.cache_path(key).exists(), (
            f"fixture missing for {claim.claim_id} at key {key}"
        )
