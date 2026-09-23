"""Cached Anthropic client. Tests never touch the network."""

import json

import pytest

from strata import llm

SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}
MESSAGES = [{"role": "user", "content": "Summarize paragraph v2:p12."}]


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path


@pytest.fixture()
def no_network(monkeypatch):
    """Any attempt to build a client fails the test."""

    def explode():
        raise AssertionError("the network was reached")

    monkeypatch.setattr(llm, "_client", explode)


def store(cache, key, response):
    (cache / f"{key}.json").write_text(json.dumps({"response": response}))


def test_cache_hit_returns_stored_response(cache, no_network):
    key = llm.cache_key(MESSAGES, "extract/v1", SCHEMA)
    store(cache, key, {"summary": "Deadline shortened."})
    out = llm.call("extract_claims", MESSAGES, SCHEMA, "extract/v1")
    assert out == {"summary": "Deadline shortened."}


def test_cache_miss_without_key_raises_naming_the_call(cache, no_network):
    with pytest.raises(llm.CacheMiss) as err:
        llm.call("extract_claims", MESSAGES, SCHEMA, "extract/v1")
    message = str(err.value)
    assert "extract_claims" in message
    assert llm.cache_key(MESSAGES, "extract/v1", SCHEMA) in message
    assert "ANTHROPIC_API_KEY" in message


def test_hash_changes_with_prompt_version():
    a = llm.cache_key(MESSAGES, "extract/v1", SCHEMA)
    b = llm.cache_key(MESSAGES, "extract/v2", SCHEMA)
    assert a != b


def test_hash_changes_with_messages():
    other = [{"role": "user", "content": "Summarize paragraph v2:p13."}]
    assert llm.cache_key(MESSAGES, "extract/v1", SCHEMA) != llm.cache_key(other, "extract/v1", SCHEMA)


def test_hash_changes_with_model():
    assert llm.cache_key(MESSAGES, "extract/v1", SCHEMA) != llm.cache_key(
        MESSAGES, "extract/v1", SCHEMA, model="claude-sonnet-5"
    )


def test_hash_is_stable_across_calls_and_dict_order():
    a = llm.cache_key([{"role": "user", "content": "x"}], "v1", SCHEMA)
    b = llm.cache_key([{"content": "x", "role": "user"}], "v1", SCHEMA)
    assert a == b == llm.cache_key([{"role": "user", "content": "x"}], "v1", SCHEMA)
    assert len(a) == 64


def test_hash_changes_with_schema(cache):
    """A schema edit must miss the cache, not return a response in the old shape."""
    wider = {
        "type": "object",
        "properties": {"summary": {"type": "string"}, "confidence": {"type": "number"}},
        "required": ["summary", "confidence"],
        "additionalProperties": False,
    }
    assert llm.cache_key(MESSAGES, "extract/v1", SCHEMA) != llm.cache_key(
        MESSAGES, "extract/v1", wider
    )


def test_schema_edit_does_not_return_the_old_cached_response(cache, no_network):
    """The end-to-end guarantee: same messages, new schema, no stale hit."""
    store(cache, llm.cache_key(MESSAGES, "extract/v1", SCHEMA), {"summary": "old"})
    assert llm.call("extract_claims", MESSAGES, SCHEMA, "extract/v1") == {"summary": "old"}

    wider = {
        "type": "object",
        "properties": {"summary": {"type": "string"}, "confidence": {"type": "number"}},
        "required": ["summary", "confidence"],
        "additionalProperties": False,
    }
    with pytest.raises(llm.CacheMiss):
        llm.call("extract_claims", MESSAGES, wider, "extract/v1")


def test_cache_path_is_named_by_hash(cache):
    key = llm.cache_key(MESSAGES, "extract/v1", SCHEMA)
    assert llm.cache_path(key) == cache / f"{key}.json"


def test_live_call_writes_request_and_response(cache, monkeypatch):
    """With a key, the response is returned and the cache file holds both sides."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    calls = {}

    class FakeMessages:
        def create(self, **kwargs):
            calls.update(kwargs)
            return type(
                "Response",
                (),
                {
                    "content": [
                        type("Block", (), {"type": "text", "text": '{"summary": "ok"}'})()
                    ],
                    "usage": type("Usage", (), {"input_tokens": 11, "output_tokens": 3})(),
                    "stop_reason": "end_turn",
                    "stop_details": None,
                },
            )()

    monkeypatch.setattr(
        llm, "_client", lambda: type("Client", (), {"messages": FakeMessages()})()
    )

    out = llm.call("extract_claims", MESSAGES, SCHEMA, "extract/v1")
    assert out == {"summary": "ok"}

    assert calls["model"] == llm.MODEL
    assert calls["output_config"]["format"]["schema"] == SCHEMA
    assert calls["output_config"]["format"]["type"] == "json_schema"
    assert calls["messages"] == MESSAGES
    assert "temperature" not in calls, "sampling params are rejected on Opus 5"

    written = json.loads(llm.cache_path(llm.cache_key(MESSAGES, "extract/v1", SCHEMA)).read_text())
    assert written["name"] == "extract_claims"
    assert written["model"] == llm.MODEL
    assert written["prompt_version"] == "extract/v1"
    assert written["messages"] == MESSAGES
    assert written["schema"] == SCHEMA
    assert written["response"] == {"summary": "ok"}
    assert written["usage"] == {"input_tokens": 11, "output_tokens": 3}


def test_second_call_hits_the_cache_written_by_the_first(cache, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    hits = []

    class FakeMessages:
        def create(self, **kwargs):
            hits.append(1)
            return type(
                "Response",
                (),
                {
                    "content": [
                        type("Block", (), {"type": "text", "text": '{"summary": "ok"}'})()
                    ],
                    "usage": type("Usage", (), {"input_tokens": 1, "output_tokens": 1})(),
                    "stop_reason": "end_turn",
                    "stop_details": None,
                },
            )()

    monkeypatch.setattr(
        llm, "_client", lambda: type("Client", (), {"messages": FakeMessages()})()
    )
    llm.call("extract_claims", MESSAGES, SCHEMA, "extract/v1")
    llm.call("extract_claims", MESSAGES, SCHEMA, "extract/v1")
    assert len(hits) == 1


def test_refusal_raises_rather_than_caching(cache, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    class FakeMessages:
        def create(self, **kwargs):
            return type(
                "Response",
                (),
                {
                    "content": [],
                    "usage": type("Usage", (), {"input_tokens": 1, "output_tokens": 0})(),
                    "stop_reason": "refusal",
                    "stop_details": type("D", (), {"category": "cyber"})(),
                },
            )()

    monkeypatch.setattr(
        llm, "_client", lambda: type("Client", (), {"messages": FakeMessages()})()
    )
    with pytest.raises(RuntimeError, match="refus"):
        llm.call("extract_claims", MESSAGES, SCHEMA, "extract/v1")
    assert list(cache.glob("*.json")) == []
