# Strata build tasks

Work these in order. Each task names the PRD criteria it satisfies, the tests to write
first, and the commit that closes it. Do not start a task until the previous one's
tests pass. Tasks 13 and 14 need a live API key; everything else runs from the cache.

## Task 1: Scaffold

PRD: none. Sets up the repo.

- Create `pyproject.toml` with dependencies: fastapi, uvicorn, jinja2, python-multipart,
  rapidfuzz, anthropic, pytest, httpx (for test client).
- Create `Makefile` with the targets in CLAUDE.md.
- Create `.gitignore` (.env, strata.db, __pycache__, .pytest_cache, .venv) and
  `.env.example`.
- Create `strata/__init__.py`, empty `tests/__init__.py`, empty `NOTES.md`.
- `make setup` and `make test` run (zero tests, exit 0).
- Commit: `scaffold: project layout, Makefile, dependencies`.

## Task 2: Models and database

PRD: R1.3 (stable IDs).

- `strata/models.py`: dataclasses Version, Paragraph, Node, Edge, Change, Claim,
  Verification, Link, Impact, ReviewTask, Event, matching TDD Section 2.
- `strata/db.py`: `connect(path)`, `init_schema(conn)`. Tables: versions, paragraphs,
  nodes, edges, events. Every table has `company_id`.
- Test first: `tests/test_db.py`: schema creates, insert and read one row of each type.
- Commit: `db: schema and record types`.

## Task 3: Ingest

PRD: R1.1, R1.2, R1.3.

- `strata/ingest.py`: `ingest_version(conn, path) -> Version`,
  `ingest_company(conn, path) -> (nodes, edges)`.
- Parses the header block, the `## Section` lines, and `[n]` paragraphs with char
  offsets. Loads nodes and edges. Idempotent. Raises on a paragraph without a marker or
  an edge to a missing node.
- Test first: `tests/test_ingest.py`: v1 yields 26 paragraphs with sequential IDs and
  correct sections; header fields parsed; meridian.json yields 31 nodes and 21 edges;
  re-ingest is a no-op; a dangling edge raises.
- Commit: `ingest: parse versions and company graph`.

## Task 4: Diff

PRD: R2.1.

- `strata/diff.py`: `diff_versions(conn, from_id, to_id) -> list[Change]`.
- Align paragraphs by number, then by similarity for unmatched ones (threshold 0.8
  ratio). Word-level SequenceMatcher for spans within modified paragraphs. Kinds:
  added, removed, modified. Unchanged paragraphs emit nothing.
- Test first: `tests/test_diff.py` from gold `changes`: v1->v2 emits changes for
  CH-1..CH-8, CH-10, CH-11 and nothing for CH-9; v2->v3 emits CH-12, CH-13, CH-15,
  CH-16, CH-17, CH-19 and nothing for CH-14, CH-18; CH-7 spans cover "thirty (30)"
  and "three (3)"; CH-15 is kind removed with from_para v2:p14.
- Commit: `diff: paragraph alignment and word-level spans`.

## Task 5: LLM client with cache

PRD: none directly; enables everything offline.

- `strata/llm.py`: `call(name, messages, schema, prompt_version) -> dict`. Hash,
  cache lookup, API call with structured output if key present, write cache, raise
  with the call name on miss without key.
- Test first: `tests/test_llm.py`: cache hit returns stored response without network;
  cache miss without key raises naming the call; hash changes when prompt_version
  changes.
- Commit: `llm: cached client`.

## Task 6: Extract (LLM call 1)

PRD: R2.2, R2.3, R2.6 (behavior measured by evals; tests cover the boundary).

- `strata/extract.py`: `build_prompt(change, context) -> messages`,
  `parse_response(raw) -> list[Claim]`, `extract_claims(change, context) -> list[Claim]`.
  Prompt marks changed spans, includes two paragraphs each side and the header,
  requires verbatim quotes inside the span, asks for version_status.
- Test first: `tests/test_extract.py`: prompt contains the marked span and the header;
  parser accepts a valid response and rejects a malformed one; a cached fixture for
  CH-7 yields two claims with obligation_change modified.
- Commit: `extract: claim extraction prompt and parser`.
- Note: the first cached fixture will be hand-written; task 13 replaces it with real
  responses.

## Task 7: Verify

PRD: R2.4, R2.5.

- `strata/verify.py`: `normalize(text) -> (text, offset_map)`,
  `verify_claim(claim, paragraphs, change) -> Verification`. Steps 1 to 6 of TDD 3.4.
- Test first: `tests/test_verify.py` driven by gold `verifier_cases` VF-1..VF-8.
- Commit: `verify: exact and fuzzy citation verification`.
- Watch for offset mapping drift after normalization; record anything that breaks in
  NOTES.md.

## Task 8: Graph

PRD: R3.2.

- `strata/graph.py`: `load(conn, events) -> Graph` (JSON nodes plus node_created and
  edge_created events), `propagate(graph, obligation_id, max_depth=3) -> list[Impact]`.
- Test first: `tests/test_graph.py` from gold `impacts` and `not_impacted`: IM-1
  reaches DOC-4 at hops 2 with path [OBL-3, PRJ-1, DOC-4]; NI-1 nodes not reached;
  cycle guard on a synthetic loop; a node added by event is reachable.
- Commit: `graph: adjacency lists and propagation`.

## Task 9: Mapping (LLM call 2)

PRD: R3.1, R3.4.

- `strata/mapping.py`: `build_prompt(claim, obligations)`, `parse_response`,
  `propose_links(claim, obligations) -> list[Link]`. Rationale quote verified against
  the obligation text using verify.py.
- Test first: `tests/test_mapping.py`: prompt lists all obligations; parser handles
  valid and malformed; cached fixture for CH-7 yields links to OBL-3 and OBL-4.
- Commit: `mapping: obligation link prompt and parser`.

## Task 10: Routing

PRD: R3.3, R4.3.

- `strata/routing.py`: `THRESHOLD = 0.7`, `ACTIONS` table,
  `route(claim, verification, links) -> (queue, reason)`,
  `create_tasks(claim, verification, links, impacts, graph) -> list[ReviewTask]`.
  One task per reached node even if reached from two obligations.
- Test first: `tests/test_routing.py` from gold `routing`: each escalation rule in
  order; RT-1..RT-8 assignees and action keys; RT-5 expert queue new_obligation.
- Commit: `routing: escalation rules and task creation`.

## Task 11: Events

PRD: R4.1, R4.2, R4.4.

- `strata/events.py`: `append(conn, event)`, `replay(conn, project_id, upto=None)
  -> State`, `rollback(conn, project_id, to_seq)`. State holds accepted claims,
  confirmed impacts, tasks by status, overrides, created nodes and edges.
- Test first: `tests/test_events.py`: replay after a fixed event list gives expected
  state; replay upto=N excludes later events; rollback appends an event and replay
  honors it; an override recorded for (change signature, OBL-3) is returned; a
  node_created event appears in state.created_nodes.
- Commit: `events: append-only log, replay, rollback`.

## Task 12: Pipeline

PRD: workflow in Section 4.

- `strata/pipeline.py`: `run_version(conn, project_id, version_id)`. Sequence:
  diff, then per change: apply override if present, extract, verify, route (skip
  mapping for created), mapping, propagate, create tasks. Event after each step.
- Test first: `tests/test_pipeline.py`: end to end v1->v2 from cache; review queue
  contains tasks for PRJ-1, DOC-1, DOC-2, DOC-4 and an expert item for CH-8; event
  count matches expected.
- Commit: `pipeline: core loop`.

## Task 13: Live run and cache population (needs API key)

- `make live`: run v1->v2, create OBL-12 through an event in a small script, run
  v2->v3. All model responses land in `data/llm_cache/`.
- Replace hand-written fixtures from tasks 6 and 9 with real cached responses. Re-run
  `make test`.
- Commit: `cache: recorded model responses for v1->v2 and v2->v3`.

## Task 14: Evals

PRD: Section 6 metrics.

- `evals/run_evals.py`: run the pipeline, compare to gold, print and write the table
  in TDD Section 4 to `evals/results.md`. Include escalation counts at 0.5, 0.7, 0.9.
- Commit: `evals: harness and first results`.
- Iterate on the extract and mapping prompts against the results. Each prompt change
  is its own commit with the new results table. Stop after two or three iterations.

## Task 15: App and templates

PRD: R4.5.

- `strata/app.py` endpoints from TDD Section 6. Templates review.html, queue.html,
  audit.html. Verification badge, confidence, path, action, assignee on every item.
  Create-obligation form on new_obligation items. Rollback control on audit page.
- Test first: `tests/test_app.py`: each endpoint 200; review page shows a verified
  badge and a path string; create-obligation post appends a node_created event.
- Commit: `app: review center, expert queue, audit page`.

## Task 16: Fresh-clone check and docs

- Clone into a new directory, follow README only, run `make setup`, `make run`,
  `make test`, `make eval`. Fix anything that fails.
- Write WALKTHROUGH.md from the code as it is. Finalize NOTES.md. Write README.md with
  setup, commands, and known limitations.
- Update PRD.md and TDD.md for anything that changed during the build; bump versions.
- Commit: `docs: walkthrough, notes, readme`.
