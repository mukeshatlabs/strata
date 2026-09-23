# Strata: standing context for Claude Code

Read this file, then PRD.md, TDD.md, and tasks.md, before doing anything in this repo.
PRD.md is the specification. TDD.md is the design. tasks.md is the build order. This
file is the working agreement.

## What this repo is

Strata is a prototype of a regulatory change-to-action workflow for a utility
regulatory affairs team. It ingests successive versions of a regulatory proceeding,
detects and classifies changes, verifies every cited passage against the source text,
maps verified changes to the company's obligations, projects, and documents, routes
review to owners, and keeps an auditable, replayable project state. PRD.md describes
the user and the requirements; TDD.md describes the design.

The code is meant to be read as much as run. Small modules with one job. Plain names.
Pure functions where possible. No clever abstractions, no framework magic, no
dependency that is not listed in TDD.md Section 1.1.

## Decisions already made (do not reopen without asking)

- Python only. FastAPI, SQLite, Jinja2 templates, pytest, difflib, rapidfuzz,
  anthropic SDK. Nothing else at runtime.
- Repository layout is TDD.md Section 1.2. Use those file and function names.
- Exactly two LLM call sites: `extract.extract_claims` and `mapping.propose_links`.
  Everything else is deterministic.
- Every LLM call goes through `llm.call`, which caches to `data/llm_cache/` keyed by a
  hash of model, prompt version, and messages. With no `ANTHROPIC_API_KEY`, the cache
  is the only source. Tests and evals run from the cache. The cache is committed.
- No embeddings, no retrieval step. The mapping call receives the full obligation list.
- No networkx. The graph is a dictionary of adjacency lists in `graph.py`.
- State is an append-only event log. Project state is computed by replay. Rollback is
  an event. Nothing is deleted.
- The graph grows through `node_created` and `edge_created` events. The JSON file is
  never modified by the application.
- Escalation threshold is 0.7, a constant in `routing.py`.
- Verifier: normalize, exact match, sliding-window Levenshtein with threshold
  `max(3, len(quote) // 25)`, wrong-paragraph check, changed-span overlap check.
  Rejection reasons: `wrong_paragraph`, `quote_outside_changed_span`, `quote_not_found`.
- Out of scope: auth, multi-tenant, PDF parsing, live docket fetch, notifications,
  integrations, multi-jurisdiction. Do not build any of these.

## The data is already written

`data/proceeding/v1.md`, `v2.md`, `v3.md`, `data/company/meridian.json`, and
`data/gold/gold.json` exist and are final. Do not edit them. If code cannot handle
something in them, fix the code. The gold file's `verifier_cases` section is the
fixture for `tests/test_verify.py`.

## How to work

- Follow tasks.md in order. One task per commit. Do not start the next task until the
  current one's tests pass.
- Test first. For each task: write the test from the PRD criterion, run it and confirm
  it fails, then implement until it passes, then commit test and code together.
- Before writing a module, state in two or three sentences what it does and its public
  function signatures, and wait for a go.
- Keep functions short. If a function is over 40 lines, split it.
- Write docstrings that say what the function takes and returns, one or two lines.
- When something breaks, append the failure and the fix to NOTES.md before moving on.
  NOTES.md also records which parts were generated and which were rewritten by hand.
- Never edit PRD.md or TDD.md silently. If the build changes a design decision, say so,
  and update the document in the same commit as the code.

## Commit discipline

- Keep full history. Never squash, never force-push, never amend a pushed commit.
- Small commits, one task or one fix each. Message format: `<area>: <what and why>`,
  for example `verify: add wrong_paragraph check before search (gold VF-4)`.
- A commit that fixes a bug references the commit that introduced it.
- Never commit `.env`, `strata.db`, `__pycache__`, or `.pytest_cache`.

## Commands (Makefile targets; keep these exact)

- `make setup`  create venv, install dependencies
- `make run`    reset db, ingest data, start the server on http://localhost:8000
- `make test`   pytest, offline, from cache
- `make eval`   run evals/run_evals.py, write evals/results.md
- `make reset`  delete strata.db and re-ingest
- `make live`   run the pipeline with a real API key to populate the cache

## Documentation (write last, from what was actually built)

- WALKTHROUGH.md: the core loop traced file by file, function by function, in the order
  `pipeline.run_version` calls them, with what each receives and returns. This is the
  onboarding document for anyone reading the code.
- NOTES.md: build log, finalized at the end.
- README.md: setup, the exact commands above, and known limitations.
