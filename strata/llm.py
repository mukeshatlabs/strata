"""Anthropic client with a disk cache (TDD 3.10).

Every model call in Strata goes through `call`. The cache is keyed by a SHA-256
of the model, the prompt version, and the message list, and is committed to the
repository, so tests and evals reproduce one recorded run with no API key and no
network. Sampling parameters are not sent: they are rejected on this model, so
the cache is what makes a run reproducible, not temperature=0.
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

MODEL = "claude-opus-5"
EFFORT = "medium"
MAX_TOKENS = 16000
CACHE_DIR = Path("data/llm_cache")


class CacheMiss(RuntimeError):
    """A call was not in the cache and no API key was available to make it."""


def cache_key(
    messages: list, prompt_version: str, schema: dict, model: str = MODEL
) -> str:
    """Return the SHA-256 hex digest identifying this call.

    Covers the model, the prompt version, the message list, and the response
    schema. The schema is included because it changes the shape of the response:
    without it, editing a schema silently returns a cached response in the old
    shape. The cost is that a schema edit invalidates those entries and needs a
    live run to record them again, which is the cheaper failure.
    """
    payload = json.dumps(
        {
            "model": model,
            "prompt_version": prompt_version,
            "messages": messages,
            "schema": schema,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_path(key: str) -> Path:
    """Return the cache file for a key."""
    return CACHE_DIR / f"{key}.json"


def _client():
    """Build an Anthropic client. Imported lazily so tests need no SDK client."""
    import anthropic

    return anthropic.Anthropic()


def _request(messages: list, schema: dict) -> tuple[dict, dict]:
    """Call the API once with a JSON-schema output format. Returns (data, usage)."""
    response = _client().messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=messages,
        output_config={
            "effort": EFFORT,
            "format": {"type": "json_schema", "schema": schema},
        },
    )
    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None)
        raise RuntimeError(f"model refused the request (category={category})")
    if response.stop_reason == "max_tokens":
        raise RuntimeError(f"response hit max_tokens={MAX_TOKENS} and is truncated")
    text = next(b.text for b in response.content if b.type == "text")
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    return json.loads(text), usage


def call(name: str, messages: list, schema: dict, prompt_version: str) -> dict:
    """Return the model's JSON response for this call, from cache when present.

    On a miss, calls the API if ANTHROPIC_API_KEY is set and records the result;
    otherwise raises CacheMiss naming the call and its key.
    """
    key = cache_key(messages, prompt_version, schema)
    path = cache_path(key)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))["response"]

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise CacheMiss(
            f"{name}: no cached response at {path} (key {key}) and no"
            " ANTHROPIC_API_KEY set. Run `make live` to record it."
        )

    data, usage = _request(messages, schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "name": name,
                "model": MODEL,
                "prompt_version": prompt_version,
                "messages": messages,
                "schema": schema,
                "response": data,
                "usage": usage,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return data
