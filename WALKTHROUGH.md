# Walkthrough: the core loop

This traces `pipeline.run_version` in the order it calls things, with what each function
receives and returns. Read it beside the code. Every path below is a real function in
`strata/`.

The worked example is `make run`: processing **v2** against **v1**.

---

## Entry: `pipeline.run_version(conn, project_id, version_id)`

**Receives** an open SQLite connection, `"proj-1"`, and `"v2"`.
**Returns** `{"from_version", "to_version", "changes", "claims", "tasks"}`, where
`tasks` is a list of `ReviewTask`.

The whole run is one transaction. Events are appended with `commit=False` and committed
once at the end; any failure rolls them back. A version run is all or nothing, so a
cache miss half way through cannot leave claims for some changes and not others, and a
retry cannot append a second copy of what already succeeded.

It is the only module that sequences stages and touches the database. Every other
module is a pure function over records.

### 1. `_prior_version(conn, "v2")` → `"v1"`

One SQL query on the `versions` table: the version whose `number` is one less. Returns
`None` for v1, and `run_version` then returns an empty summary, because a first version
has nothing to diff against.

### 2. `diff.diff_versions(conn, "v1", "v2")` → `list[Change]`

Deterministic. No model.

Loads both versions' paragraphs in order and runs `difflib.SequenceMatcher` over the
**sequence of paragraph texts**, treating each paragraph as one opaque element. This is
what makes renumbering free: when `v2:p17` is inserted, `v1:p17..p19` comes back as an
`equal` block aligned to `v2:p18..p20` and emits nothing.

Every `SequenceMatcher` in the module passes `autojunk=False`. The default heuristic
treats common characters in long sequences as junk and drops the alignment score of one
gold pairing from 0.80 to 0.35.

Inside a `replace` block: equal length pairs by position with no similarity floor,
because a paragraph can be replaced wholesale and still be a modification. An unequal
block, where one side must drop out, is paired by similarity at 0.6. Then a second,
word-level `SequenceMatcher` inside each modified paragraph produces `spans_from` and
`spans_to`, the character ranges that changed.

For v1 → v2 this yields **14 changes**: 12 modified, 1 added (`c:v1->v2:+p17`), 1
removed (`c:v1->v2:-p25`).

### 3. `events.replay(conn, "proj-1")` → `State`

Folds the whole event log into project state. What `run_version` wants from it is
`state.overrides`, the corrections a human has made, so the model is not asked about
something already decided.

`replay` resolves rollbacks first, in `events.effective`, because a rollback undoes
events that *precede* it in the log. Rollback events are dropped from the fold and kept
in `events.history` for the audit page.

### 4. `graph.load(conn, events.effective(conn, project_id))` → `Graph`

The company graph is the `nodes` and `edges` tables as ingested from
`data/company/meridian.json`, plus every `node_created` and `edge_created` event since.
The JSON file is never written by the application. `Graph` holds `nodes` by ID and
`incoming`, mapping each node to the edges arriving at it.

Obligations are then filtered out of `graph.nodes` and sorted by number. On a v3 run
this list includes `OBL-12`, which exists only because the log says so.

### 5. `_paragraphs(conn)` → `dict[para_id, text]`

Every paragraph of every version, for the verifier to search.

---

## Per change: `extract.extract_claims(conn, change)` → `list[Claim]`

**LLM call 1**, one per change. Three steps:

`context_for(conn, change)` reads the version header block and the two paragraphs either
side of each changed paragraph.

`build_prompt(change, context)` returns the messages list. It renders both versions of
the paragraph with the changed spans wrapped in `<<` and `>>`, the neighbours labelled
as context and explicitly not quotable, and the rules the verifier will enforce: copy
the quote character for character, include text from inside a marked span, the markers
are not part of the document text and must not appear in the quote, and name the
paragraph the quote came from. It also carries the one-line definition of material that
the gold traps turn on, and the rule that a paragraph summarizing a duty imposed
elsewhere is not itself an obligation change.

`llm.call("extract_claims", messages, SCHEMA, PROMPT_VERSION)` hashes the model name,
the prompt version, the messages and the response schema to SHA-256 and looks for
`data/llm_cache/<hash>.json`. On a hit it returns the recorded response. On a miss with
`ANTHROPIC_API_KEY` set it calls the API with a JSON-schema output format and records
the result; on a miss without a key it raises `CacheMiss` naming the call. A refusal or
a truncation raises rather than being cached, so a failure is never replayed as a real
response.

`parse_response(raw, change)` validates the JSON and returns `Claim` records, minting
`claim_id` as `<change_id>:k1`, `:k2`. It rejects a `quote_para_id` the change does not
span, which is what caught a malformed test stub during the build.

For `c:v1->v2:p12` this returns **two claims**, because that paragraph alters two
duties: `k1`, the study deadline shortened from forty-five to thirty business days, and
`k2`, the delivery deadline shortened from five to three. One claim per obligation
altered is what PRD R3.4 asks for, and it is the reason tasks have to be merged for the
whole change further down.

---

## Per claim: `pipeline._run_claim(...)` → `list[ReviewTask]`

These tasks are returned, not written. `run_version` collects every claim of one change
and merges them before any `task_created` event is appended; see the merge step below.

### a. `events.append(conn, claim_extracted)`

Every stage writes an event. `events.append` is the only write path for anything that
changes project state, and it returns the event with its assigned sequence number.

### b. `verify.verify_claim(claim, paragraphs, change)` → `Verification`

No model. This is the step the product's trustworthiness rests on.

1. **Paragraph check, before any search.** A `modified` change may be quoted from either
   side, because the changed text of a pure word deletion exists only in the previous
   version; an `added` change has only a to-side, a `removed` change only a from-side.
   Anything else is `rejected` with reason `wrong_paragraph` and no search runs. Doing
   this first is what separates a real quote lifted from the neighbouring paragraph from
   a quote that does not exist anywhere.
2. **Normalize**, via `verify.normalize`, which unifies curly quotes, dashes and
   non-breaking spaces one character for one, then collapses runs of whitespace. It
   returns the normalized text *and* an index map back to the original, so every offset
   the verifier reports is in the original paragraph's coordinates.
3. **Exact find** → `verified`, distance 0.
4. Otherwise **sliding-window Levenshtein** (`rapidfuzz`) at `max(3, len(quote) // 25)`,
   recording the distance **and the offset of the winning window** → `near`.
5. **Overlap**: the match must intersect a changed span on the side the quote came
   from, or it is `rejected` with `quote_outside_changed_span`. A side with no changed
   spans can overlap nothing and rejects.
6. Otherwise `rejected` with `quote_not_found`.

Then `events.append(conn, claim_verified)`.

### c. Three gates, in this order

**Rejected citation** → straight to `routing.create_tasks` with no links and no
impacts, which routes to the expert queue by rule 1, and stop. A rejected quote on a
real change is still a change someone must look at, so this is decided before
materiality.

**Not material** → return no tasks. Recorded, no further action.

**`obligation_change == "created"`** → `links = []`, skipping LLM call 2 entirely. No
existing node can be the right link for a duty that did not exist.

### d. `_overrides_for(state, change, obligations)` → `list[Link]`

Before the model is asked. For each candidate obligation it calls
`events.override_for(state, change, obligation_id)`, which tries two keys:

- `events.signature(change)`, a hash of the change's kind and both normalized texts with
  no paragraph ID and no version in it, so it survives renumbering;
- `events.lineage_key(change.text_from)`, which matches a correction stored under the
  *to*-text of an earlier change. This is what carries a v2 correction into a v3 re-edit
  of the same paragraph: the v3 edit has a different signature, but its from-text is
  exactly what v2 produced.

If any correction matches, those links are used and LLM call 2 never happens.

### e. `mapping.propose_links(claim, obligations)` → `list[Link]`

**LLM call 2**, only for a material, verified, non-created claim with no override.

`build_prompt` lists every obligation as ID, name and text, states that an empty list is
the correct answer when none fits, and warns that two obligations sharing a number are
not for that reason the same obligation — `OBL-2` and `OBL-7` both contain "thirty (30)
days", next to this change's "thirty (30) business days".

`parse_response` validates each link, drops one naming an obligation outside the
candidate list with a logged warning rather than raising, and checks `rationale_quote`
against that obligation's own text with `verify.find_quote`, the same normalize and
search `verify_claim` uses. The result goes in `rationale_status`. A failed rationale
does not drop the link; it is review material, not a gate.

Then one `link_proposed` event per link.

### f. `graph.propagate(company, link.obligation_id)` → `list[Impact]`

Breadth-first over **incoming** edges. Every edge in the company graph points toward an
obligation or a project, so the walk runs in reverse: from the changed obligation out to
whatever points at it. Each `Impact` records the node, its type, the full path, the edge
types along it, the hop count and the node's owner. A visited set guards cycles; BFS
means each node is recorded by its shortest route.

From `OBL-3`: `PRJ-1` and `DOC-1` and `DOC-2` at one hop, and `DOC-4` at two, via
`["OBL-3", "PRJ-1", "DOC-4"]` — the capital plan, reachable only through the project.

One `impact_found` event per impact, with `claim_id` merged into the payload, because
`Impact` has no such field and `replay` keys impacts on it.

### g. `routing.create_tasks(claim, verification, links, impacts, company)` → `list[ReviewTask]`

`routing.route` applies five escalation rules in order, then falls through to the owner
queue, and returns `(queue, reason)`:

1. rejected citation → expert, with the verifier's own reason
2. any link whose `rationale_status` is `rejected` → expert, `rationale_unverified`
3. `created` → expert, `new_obligation`
4. any link below `THRESHOLD = 0.7` → expert, `low_confidence`
5. a modified or removed duty with no confident link → expert, `no_confident_link`
6. otherwise the owner queue

An escalated claim produces **one** expert task with no node, assigned to `P-8`,
regulatory counsel. No owner tasks are created even where impacts exist: nothing reaches
an owner until a human has agreed the claim is sound.

Otherwise one task per reached **node** — the linked obligations themselves, which
propagation does not return, plus everything reached from them. The recommended action
comes from the `ACTIONS` table keyed on `(obligation_change, node_type)`.

### h. `routing.merge_tasks(tasks_for_this_change)` → `list[ReviewTask]`

Back in `run_version`, once every claim of the change has been processed.

`c:v1->v2:p12` produced two claims, and each of them reached `PRJ-1`, `DOC-1` and
`DOC-4` through its own obligation. That is one piece of work per node for one owner,
not two, so owner tasks are merged by node: the task id becomes
`<change_id>:<node_id>`, `paths` holds every route that reached it
(`[["OBL-3","PRJ-1"], ["OBL-4","PRJ-1"]]`), `obligation_ids` holds both obligations, and
`claim_ids` holds both claims, so approving the merged task accepts both.

Expert tasks are not merged. Each carries its own claim's escalation reason, and two
claims of one change can fail for different reasons.

Then one `task_created` event per merged task.

---

## What the screen shows

`app.py` renders every page from `events.replay` on each request, so the review center
and the audit trail cannot disagree. The shared layout's sidebar, listing each version
with what has been done to it and the open task counts, comes from that same replay, so
it is correct on whichever page you are on.

`orientation(state, versions)` turns the state into the banner at the top of the review
center: one paragraph per processed version pair, newest first, each naming the versions
compared and the counts, plus one next-step sentence chosen by rule — resolve the expert
queue while a new obligation is unresolved, otherwise load the next version, otherwise
nothing is left. Nothing in it is written per version, so a fourth version file would
produce a correct banner with no template change.

Within a pair, changes with a material claim are listed first and the rest are collapsed
behind one counted summary line, because the not-material changes are the majority and
are the ones a reviewer has already decided not to care about.

Every version has a page of its own, rendered from the `versions` and `paragraphs`
tables rather than re-read from the file, so what is on the screen is what the verifier
searched. Each paragraph carries its ID as an anchor, and a claim's `quote_para_id` on
the review center links to it, which turns the check the verifier performs mechanically
into one click for the person who has to sign the memo. Approve, edit and escalate append events;
**edit** additionally computes the change signature and the lineage key and writes them
into the payload, which is what makes the correction findable in the next version.
Rollback appends a `rollback` event, and `replay` ignores everything after its `to_seq`
from that point on. Nothing is ever deleted.
