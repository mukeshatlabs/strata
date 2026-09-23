# Build notes

Failures, fixes, and what was generated vs rewritten by hand.

## Task 3: ingest

Decision, not a failure: TDD 2.1 says paragraph `char_start`/`char_end` are offsets
"within the file" but does not say where a paragraph starts relative to its `[n]`
marker. Pinned it as: `text` excludes the marker, and `char_start` is the file offset
of the first character of `text`, so a paragraph-local offset `p` is file offset
`char_start + p` with no correction term. `test_char_offsets_index_the_file_exactly`
asserts `text == file[char_start:char_end]` for all 77 paragraphs across v1-v3.

Verification never reads these fields. `verify.py` searches inside a paragraph's text
and compares the match against `change.spans_*`, both paragraph-local (TDD 2.2, 3.4);
the file offsets exist so a claim can point into the source document (PRD R1.3).

Footnotes are themselves numbered paragraphs (`[24] [^1] Docket No. ...`). The marker
regex is anchored `^\[(\d+)\]` so the inline `[^1]` survives into the text, which the
footnote-renumbering trap (CH-9, CH-14) depends on.

## Task 4: diff

**autojunk.** `difflib.SequenceMatcher` enables its `autojunk` heuristic by default,
which treats any element occurring in more than 1% of a sequence of 200 or more
elements as junk. On character sequences that means spaces and common letters, and it
is catastrophic for paragraph similarity: the CH-16 pairing (`v2:p15` -> `v3:p14`)
measures 0.802 with `autojunk=False` and 0.348 with the default, and `v2:p14` ->
`v3:p14` drops from 0.501 to 0.013. Every SequenceMatcher in `diff.py` passes
`autojunk=False`, and `test_similarity_uses_autojunk_false` asserts the default would
fall below the threshold.

**Alignment rule.** tasks.md originally said "align by number, then by similarity
(threshold 0.8)". Aligning by number first is wrong here: v2:p17 is an insertion, so
number-aligning v1:p17 against v2:p17 would pair unrelated text and cascade spurious
changes through the rest of the document. Replaced with difflib over the paragraph
sequence. The similarity threshold is also wrong applied to equal-length blocks: gold
CH-17 pairs at 0.792 and CH-19 at 0.218, and both must be `modified`. Similarity now
decides only unequal blocks, where one side has to drop out, and the threshold moved
from 0.8 to 0.6 because the accepted CH-16 pairing sits at 0.802 and the rejected
competitor at 0.501. tasks.md and TDD 2.2 updated in the same commit.

**change_id collision.** Keying the ID on the to-paragraph collides in the v2->v3
block `[v2:p14, v2:p15] -> [v3:p14]`: the removal and the modification both render
`c:v2->v3:p14`. IDs now mark the kind: `p15` modified, `+p17` added, `-p14` removed.

**Failure: over-strict span assertion.** First run of `test_diff.py` failed on
`c:v1->v2:p1` asserting both span lists were non-empty. A modification whose word diff
is a pure insertion has no from-spans (`v2:p1` adds "REVISED"), and a pure deletion has
no to-spans (`c:v2->v3:p7` drops "proposed"). The invariant is that at least one side
is non-empty. Test corrected, code unchanged.

Consequence for task 7: `c:v2->v3:p7` is a modification with `spans_to == []`, so a
quote from it can overlap nothing and the TDD 3.4 step 5 overlap check rejects
everything in that paragraph. Decide there whether an empty span list on the quoted
side means "reject" or "the whole paragraph is the changed region".

**Volume.** 14 changes for v1->v2 and 13 for v2->v3, against the 11 and 8 rows in gold.
The extra ones are real ("revised" inserted, hearing dates, footnote renumbering) and
are the not-material cases the model has to reject in task 6.

## Decision for task 7: which side a quote may come from

Settled before building the verifier, and TDD 3.4 is to be updated when task 7 lands.

For a `modified` change the quote may come from either the to-paragraph or the
from-paragraph. The verifier reads `quote_para_id`, checks it names one of the two
paragraphs the change spans, and requires the match to overlap *that side's* spans:
`spans_to` when the quote names `para_id_to`, `spans_from` when it names
`para_id_from`. A quote naming any other paragraph is `wrong_paragraph`.

An `added` change has only a to-side and a `removed` change only a from-side, so for
those the quote must name that paragraph and overlap that side's spans.

This resolves the `c:v2->v3:p7` case from task 4. That modification is a pure word
deletion ("The proposed rule applies" -> "The rule applies"), so `spans_to` is empty
and `spans_from` is `[(4, 13)]`. A quote naming `v3:p7` can overlap nothing and is
rejected; a quote naming `v2:p7` and containing "proposed" verifies. The changed text
exists only on the from-side, and the rule now says so rather than rejecting both.

Gold VF-7 is the same shape from the other direction: a removal whose quote must be
verified against the from-version, since the text is gone from v3.

## Task 5: llm client

**Model ID.** `claude-opus-5`, in one constant at the top of `llm.py` alongside
`EFFORT` and `MAX_TOKENS`. Taken from the current Claude API reference rather than
from memory, because the ID is part of the cache hash and every committed cache
filename depends on it. The ID is complete as written and takes no date suffix.

**Structured output.** `output_config={"format": {"type": "json_schema", "schema": ...}}`
on `messages.create()`. The older top-level `output_format` parameter is deprecated.
The first text block of the response is guaranteed to be valid JSON against the schema,
so the parser is `json.loads`, not prose scraping (TDD 3.3).

**No sampling parameters.** `temperature`, `top_p` and `top_k` are rejected on this
model. Reproducibility across runs therefore comes from the committed cache and
nothing else, which is the reason TDD 3.10 commits it. A live run is not expected to
reproduce a previous live run byte for byte.

**Schema is in the cache key.** First written the other way, following TDD 3.10's
literal list of model + prompt version + messages, on the argument that hashing the
schema makes a cosmetic edit cost a paid live run. Reversed on review: a stale cache
entry is worse than an occasional re-run, because a schema edit would otherwise return
a response in the old shape and nothing would report it, whereas a re-run is visible
and bounded. The key now covers model, prompt version, messages, and schema. TDD 3.10
updated in the same commit. `test_schema_edit_does_not_return_the_old_cached_response`
pins the end-to-end guarantee, not just the hash.

**Key detection.** `ANTHROPIC_API_KEY` only. The SDK would also resolve an
`ant auth login` profile, so an unset variable does not strictly mean no credentials,
but CLAUDE.md makes the env var the switch and tests must never reach the network.
`_client` is imported lazily and is only reached on a cache miss with a key set, so an
offline run never constructs a client.

**Refusal and truncation raise rather than cache.** A `stop_reason` of `refusal` or
`max_tokens` means there is no valid response to record; writing one would poison the
committed cache with a failure that later runs would replay as though it were real.

## Task 6: extract

**Signatures differ from tasks.md.** `parse_response(raw, change)` takes the change
because the model never sees or invents IDs; `claim_id` and `change_id` are minted
here (`c:v1->v2:p12:k1`). `context_for(conn, change)` was added so the database read
stays out of `build_prompt` and `parse_response`, which are pure and testable with no
connection. `extract_claims(conn, change)` takes a connection for the same reason.

**Two prompt lines earn their place.** The definition of material ("a change is
material if it alters a duty, deadline, threshold, penalty, scope, or applicability;
wording, numbering, status language, procedural dates, and background are not
material") is what the gold traps turn on: CH-2 and CH-3 are footnote renumbering,
CH-6 is a cosmetic rewording of the same 10-day duty, CH-5 mentions penalties in a
summary paragraph while the duty itself lives in p17. And the `<<`/`>>` markers are
declared not part of the document text and forbidden from the quote, or the model
copies them in and the verifier rejects a quote that was otherwise correct.

**Quotes overlap the span, not sit inside it.** Gold VF-1 verifies "shall complete the
interconnection study for a small storage resource within thirty (30) business days",
which is far longer than the marked span `<<thirty (30) >>`. TDD 3.4 step 5 requires
the match to *overlap* a changed span, so the prompt says the quote must include text
from inside a marked span and may extend past it in either direction. Saying
"inside the span" would have produced two-word quotes that verify but read as nothing.

**version_status is per response, not per claim.** The schema returns one
`version_status` for the version and a list of claims; `parse_response` copies it onto
every Claim per TDD 2.3. Asking for it per claim invites the same version to be called
draft and final in one response.

**Failure: prompt assertions were coupled to line wrapping.**
`test_prompt_says_the_markers_are_not_part_of_the_text` failed because the sentence
wraps across a newline in the source. Fixed in the test by collapsing whitespace before
asserting, not by reflowing the prompt: the prompt's wording is hashed into the cache
key, so tests must not push it around for cosmetic reasons.

**The CH-7 fixture is hand-written and is held to the prompt's own rules.** The
generator asserts each quote appears verbatim in `text_to` and contains no `<<`/`>>`
before writing. The file carries a `note` field saying it is hand-written, and task 13
replaces it. `test_cache_key_matches_the_committed_fixture` fails loudly with the new
key if the prompt or the schema changes, which is the signal to regenerate.

## Task 7: verify

**Gold-set defect found, and the one permitted data edit.** VF-8 exists to prove that
normalization works: its `tests` field reads "source uses curly quotes / non-breaking
spaces; quote uses straight quotes / regular spaces; must still verify exactly after
normalization". But its `source_text_override` was byte-identical to its `quote`, both
plain ASCII, and the three version files contain no non-ASCII characters at all. The
row passed without normalization existing, which made it a vacuous green on the one
behaviour it was written to test. Fixed with approval by putting a real non-breaking
space inside the quoted span, curly quotes and an em dash outside it, and a doubled
space before it so offsets shift under collapse. `quote` is untouched, and
`test_vf8_gold_row_actually_exercises_normalization` now asserts the row contains the
characters its description claims and that the quote does *not* match before
normalization, so the row cannot silently go vacuous again.

**Offset map.** `normalize` returns the text plus a list with one entry per normalized
character and a final entry holding `len(text)`, so a normalized span `[s, e)` maps to
`[map[s], map[e])`. Character-for-character substitutions leave offsets alone; only
whitespace collapse is many-to-one, and it emits its space at the index of the run's
first character. Matching runs in normalized coordinates and every offset reported is
mapped back, so `match_start` and `match_end` are comparable to `Change.spans_*` and
can highlight the real source text. Tested three ways: a hand-written expected map for
a string using every substitution, a round-trip over real paragraph text, and VF-8,
where the matched original text is deliberately *not* equal to the quote and only
becomes equal after normalization.

**Order of checks.** The paragraph check is now step 1 in both code and TDD 3.4, which
previously listed it fourth while its prose said it runs first. VF-3 and VF-4 carry the
same quote and differ only in `quote_para_id`; if the search ran first, VF-4 would
report `quote_not_found` and the distinction the gold set exists to test would be gone.
The test asserts `match_start is None` on a `wrong_paragraph` rejection, which is the
observable proof that no search happened.

**Near matches record the winning window.** The sliding window tracks the best distance
*and its offset*. Without the offset, `match_start` on a near match would be
meaningless and the step 5 overlap check could not run on a near match at all. VF-2
asserts `match_start > 0` and that the recovered window starts at the real sentence.

TDD 3.4 rewritten in this commit for the either-side quote rule and the step order.

## Task 8: graph

**Propagation is a reverse walk, and the data forces it.** Every edge in
meridian.json points toward an obligation or a project: `project -depends_on->
obligation` (5), `document -implements-> obligation` (13), `document -references->
project` (3). No edge originates from an obligation. So `propagate` walks *incoming*
edges, from the changed obligation outward to whatever points at it; a forward walk
from OBL-3 would return an empty list and every gold impact row would fail.
`test_every_edge_points_toward_an_obligation_or_project` asserts the premise rather
than leaving it as a comment, so a later edge type pointing the other way fails here
instead of silently halving the impact set.

**Event payload shapes are fixed in TDD 2.5, not in each module.** `node_created`
carries `id`, `type`, `name`, `text`, `owner`, `source_para`; `edge_created` carries
`from`, `to`, `type`. `graph.load` reads exactly those keys and task 11's
`events.append` writes them. The test builds the payload dicts literally rather than
calling a helper, so the shape is asserted from the outside and the two modules cannot
drift apart through a shared constructor.

**An edge_created to an unknown node is ignored, not an error.** The log is
append-only and replay must not fail on an event that a later rollback makes
irrelevant (TDD 3.9). Raising here would make the audit page unopenable after a
rollback, which is the opposite of what the event log is for.

**BFS gives shortest paths, which is what the gold expects.** DOC-4 reaches OBL-3 only
through PRJ-1 and is recorded at two hops with path `["OBL-3", "PRJ-1", "DOC-4"]`
(IM-1's trap). `test_shortest_path_wins_when_a_node_is_reachable_twice` adds a direct
DOC-4 -> OBL-3 edge by event and asserts the one-hop route wins, so a change to
depth-first order would be caught.

**The created node is not written to the nodes table.** `graph.load` builds it in
memory from the log every time, and a test asserts the table still has no OBL-12 row.
The JSON file is unchanged too, asserted by reading it before and after. The node
exists because the log says it does, which is the property TDD 3.9 is claiming.

**IM-2 leaves a task 10 problem.** PRJ-1 and DOC-1 are each reached from both OBL-3
and OBL-4 for the same change CH-7, so propagation returns them twice with different
paths. That is correct here: each Impact records the obligation it came from. Task 10
must create one task per reached node, not one per impact, or CH-7 produces duplicate
work for the same owner.

## Task 9: mapping

**`verify.find_quote` extracted, not duplicated.** The rationale quote has to be checked
against an obligation's text, where there is no paragraph and no change record, so the
search half of `verify_claim` is now `find_quote(quote, text) -> (status, start, end,
distance)` and `verify_claim` calls it. Copying the normalize-and-search logic into
mapping would have let the two drift, and the verifier's behaviour is the product's
main claim; there is one implementation of it.

**An unknown obligation_id is dropped, not raised.** A response naming OBL-999
alongside a good link should not discard the good link, and the run should not fail:
this is one claim out of dozens in a pipeline pass. It is logged with the claim ID and
the candidate count so the eval run shows it. Raising would turn a model slip into a
pipeline crash; silently ignoring it would hide a prompt regression.

**A failed rationale quote does not drop the link.** `rationale_status` records
verified, near or rejected and the link survives. The rationale is review material, not
a gate: the link itself may well be right while the quote supporting it was invented,
and that combination is exactly what a reviewer should see. The confidence threshold in
task 10 is the gate.

**Prompt licenses an empty answer.** The failure mode here is forcing a link to the
closest-looking obligation, which for CH-8 (a new penalty duty with no matching node)
would silently attach a new obligation to an unrelated existing one instead of
escalating. The prompt says an empty list is a correct answer and that a shared number
is not a match: OBL-2 and OBL-7 both contain "thirty (30) days" and sit next to CH-7's
"thirty (30) business days". A test asserts those two are still the distractors, so if
the data changed the prompt gets revisited.

**Failure: a test of my own making.** `test_find_quote_needs_no_change_record` asserted
a trailing-period quote would be `near`, but the sample text I wrote ended in "days."
so the quote matched exactly and returned `verified`. The test was wrong, not the code;
the sample text now continues past the quote.

**Fixtures.** Two hand-written mapping fixtures, one per CH-7 claim, k1 to OBL-3 and k2
to OBL-4, so the union satisfies PRD R3.4. The generator asserts each rationale quote
appears verbatim in its obligation's text before writing. Both are marked hand-written
and are replaced in task 13.

## Task 10: routing

**New rule: `rationale_unverified`.** Added at position 2, immediately after the
citation rule and before confidence. Task 9 keeps a link whose rationale quote failed
to verify, because the link may be right while the quote supporting it was invented;
this is where that combination becomes visible instead of silently reaching an owner. A
`near` rationale is tolerated, matching PRD 9's treatment of near citations. Its
position relative to the `created` rule is immaterial in practice, because a created
claim never reaches the mapping call and so has no links, but it is placed where the
instruction put it.

**The action table needed obligation rows.** `propagate` returns what a change reaches
and deliberately excludes the obligation it started from, so with only project and
document entries a modified duty produced tasks for every downstream document and none
for the person who owns the duty. CH-7 would have created work for DOC-1, DOC-2 and
DOC-4 and nothing for P-2 on OBL-3 and OBL-4. `create_tasks` now seeds the task set
with the linked obligations themselves, each with `paths == [[obligation_id]]`.

**Dedupe keeps the routes.** One task per node keyed on `node_id`, but every path that
reached it is kept. CH-7's PRJ-1 task carries `[["OBL-3","PRJ-1"], ["OBL-4","PRJ-1"]]`
and `obligation_ids == ["OBL-3","OBL-4"]`; DOC-2 carries only the OBL-3 route because
only OBL-3 reaches it. `ReviewTask` gained `paths` and `obligation_ids`, and TDD 2.5 is
updated. Without this the review page could say a document is affected but not say by
which of the two changed duties, which is the question a reviewer asks first.

**An escalated claim creates no owner tasks at all, even though impacts exist.**
`create_tasks` returns a single expert task with `node_id` None. PRD R4.3 says an
escalated item is not applied until a human approves, and creating owner tasks
alongside the expert task would put unreviewed work in six people's queues.

**Gap found, then closed: there was no expert in the company graph.** PRD 2 describes
experts as senior reviewers, "typically regulatory counsel or the director", and says
owners and experts are assignees in the prototype. `meridian.json` had P-1 to P-7:
Dana plus six owners, no counsel and no director, so expert tasks initially carried
`assignee = None` with the queue itself as the addressee. Assigning them to Dana would
have conflated the analyst with the reviewer the PRD distinguishes her from.

Closed by adding **P-8, Marcus Reyes, regulatory counsel**, with approval. This is the
second permitted edit to `data/`, after the VF-8 fix in task 7. P-8 owns no obligation,
project or document and has no edges: the node exists only to be the expert queue's
assignee, which is what PRD 2 describes. `routing.EXPERT = "P-8"` is the single place
the ID appears. Node count moves from 31 to 32 and the person count from 7 to 8;
`tests/test_ingest.py`, `tasks.md` and TDD 3.8 updated to match.

## Task 11: events

**Change signature is content-keyed.** `signature(change)` hashes the kind and both
normalized texts and contains no paragraph ID and no version. Paragraph IDs do not
survive renumbering, and renumbering happens in every version here: removing `v2:p14`
shifts every later ordering paragraph by one, so an ID-keyed override would treat CH-16
and CH-17 as new work. Normalization runs through `verify.normalize`, the same function
the citation check uses, so a whitespace or curly-quote difference does not orphan a
correction.

**Overrides are stored under two keys.** The edit signature catches the same edit seen
again. The lineage key, a hash of the normalized to-text of the corrected change,
catches the case that actually matters: v2 produces some text, Dana corrects the mapping
for it, and v3 edits that same text again. The v3 change has a different edit signature
because its to-text is new, but its from-text is exactly what v2 produced, so the
lineage key matches and the correction is applied before the mapping model is called.
`test_override_carries_into_a_later_edit_of_the_same_paragraph` constructs that pair
directly and asserts the signatures differ while the override is still found.

The chain is deliberately one generation long.
`test_lineage_does_not_match_a_third_generation_by_accident` asserts an unrelated later
change finds nothing: an override follows the text it corrected into the next edit of
that text, not forward forever.

**Rollback is resolved in its own pass.** A rollback undoes events that precede it in
the log, so `effective()` walks the history once and drops already-collected events
above the rollback's `to_seq` before anything is folded. Nested rollbacks then need no
special casing, and events appended *after* a rollback still apply, both tested.
`history()` keeps the rollback events so the audit page can show them; `effective()`
drops them because they are not state.

**Acceptance requires a human.** A claim enters `accepted_claims` and its impacts enter
`confirmed_impacts` only on `task_approved` or `task_edited`, never on extraction or
routing. PRD R4.3 says an escalated or unreviewed item is not applied to project state
until a human approves it, and this is where that is enforced rather than assumed.

## Task 12: pipeline

**Bug: the `impact_found` payload was missing `claim_id`.** `events.replay` keys impacts
on `claim_id`, but `Impact` has no such field: it records the obligation it came from,
not the claim. Writing `impact.__dict__` straight into the event raised `KeyError` on
the next replay. Found by `test_tasks_are_replayable_into_state`, which replays after a
run rather than trusting the return value. Fixed by merging `claim_id` into the payload
at the write. Worth noting for the walkthrough: a dataclass and its event payload are
not the same shape, and only a replay test catches the difference.

**Failure: the test stub's discriminator was too loose.** The stub answered "this is
CH-7" for any prompt containing "Study completion." and "thirty (30)", which is also
true of CH-6's prompt, because `context_for` includes two paragraphs either side and
`v2:p12` is two after `v2:p10`. The stub then returned a claim quoting `v2:p12` for a
change spanning `v1:p10` and `v2:p10`, and `extract.parse_response` correctly rejected
it as a quote from a paragraph the change does not span. Fixed in the stub by keying on
the changed-paragraph label the prompt emits. The parser's paragraph check caught a
malformed test before it could produce a misleading pass.

**Failure: a test assumed a global stage order.** `test_stages_run_in_order` asserted
the last model call was the mapping call. CH-7 is the seventh of fourteen changes, so
extraction continues after its mapping call: the loop is per change, not per stage. The
test now asserts the interleaving that actually holds, that mapping follows its own
extraction and that later changes are still extracted afterwards.

**Order of the gates.** A rejected citation is routed before materiality is consulted,
because TDD 3.4 says a rejected quote on a real change is still a change someone must
look at. A non-material claim stops after verification with no mapping call and no
tasks. A `created` claim skips the mapping call entirely.
`test_created_claim_skips_the_mapping_call` makes the stub raise if mapping is called,
so the skip is asserted by the absence of a call rather than by the shape of the result.

**Overrides are consulted before the model.** `_overrides_for` walks the candidate
obligations and looks each one up by both keys from task 11. If any correction matches,
those links are used and `llm.call` never happens, which is what PRD R4.4 asks for: not
re-proposed, and not re-asked. The test asserts `propose_links` is absent from the call
log, not merely that the result contains OBL-5.

**The end-to-end test skips rather than fails.** It runs the real cache with no key and
calls `pytest.skip` with the missing key and the `make live` hint when a response is not
yet recorded. Task 13 populates the cache and the test starts running on its own.

**CLI added.** `python -m strata.pipeline ingest|run|live`, which the Makefile has
referenced since task 1. `make reset` now works: 3 versions, 77 paragraphs, 32 nodes,
21 edges.

## Task 13: live run and cache

**API constraint found on the first call: `minimum` and `maximum` are not supported on
a `number` in a structured-output schema.** The first `make live` died with
`400 invalid_request_error: output_config.format.schema: For 'number' type, properties
maximum, minimum are not supported`. Both schemas declared `confidence` as
`{"type": "number", "minimum": 0, "maximum": 1}`. Removed from the schema; the 0 to 1
range was already enforced in `parse_response` in both modules, so nothing is
unchecked, and the parser is the better place for it anyway because a violation then
names the claim. This changed both schemas, and the schema is in the cache key, so the
three hand-written fixtures were orphaned by it before being deleted.

**The run.** 36 calls: 27 extractions, one per change, and 9 mapping calls, one per
material claim that was not a `created` duty. 66,761 input tokens and 8,000 output
tokens, about $0.53. v1 to v2 produced 16 claims and 12 tasks; v2 to v3 produced 19
claims and 24 tasks.

**Citation fidelity: 34 verified, 0 near, 1 rejected.** The single rejection is
`c:v2->v3:p7` with reason `quote_outside_changed_span`, which is exactly the case
predicted in task 4's notes and settled by the quote-side rule before task 7. That
change is a pure word deletion, "The proposed rule applies" becoming "The rule
applies", so `spans_to` is empty and a quote naming `v3:p7` can overlap nothing. The
model quoted the to-side. The verifier rejected it and routed it to the expert queue,
which is the designed behaviour, but it is also the clearest candidate for a prompt fix
in task 14: the prompt should say that when the changed text exists only in the
previous version, quote from there.

**Materiality: 15 of 16 gold changes correct.** The miss is CH-5, the trap gold
describes as "summary paragraph mentions penalties; the obligation itself is in p17,
not here". The model returned material with `obligation_change: created` and confidence
0.92, summarising that the revised rule "adds penalties for missed study deadlines".
The materiality line in the prompt covers what counts as material but says nothing
about a paragraph that *describes* a duty imposed elsewhere. That is the task 14 prompt
iteration.

CH-16 and CH-17 each produced an extra `created` claim alongside the expected
`modified` one. Gold CH-16 already allows an extra link to OBL-6; the extra `created`
claims are a precision cost, not a miss, and each one lands in the expert queue rather
than reaching an owner.

**Draft/final: v1 to v2 unanimous draft, correct. v2 to v3 split 17 final to 2 draft,
majority correct.** Because `version_status` is asked per change, a version gets one
vote per change and they can disagree. The eval in task 14 has to define how a version
status is decided from the votes; a majority is the obvious rule and the split is worth
reporting rather than hiding.

**OBL-12 was created between the runs the way the review center will.** The live flow
approves the escalated `new_obligation` tasks, appends `node_created` for OBL-12 with
owner P-2 and `source_para` v2:p17, and appends `edge_created` for DOC-1 implements
OBL-12. This conflicts with gold IM-5, which states OBL-12 "has no edges yet; impacts
empty". The edge was added on instruction so CH-17 has something to propagate through.
Edges never enter a prompt, so this affects no cached response and can be reverted
without a live run; it will show as an IM-5 failure in the task 14 evals unless the
edge is dropped or the gold row is revisited.

## Task 14: evals

**The harness drives the pipeline rather than re-implementing it.** The first version
built its own extract-verify-map loop and immediately missed the cache: it used one
obligation list for both version pairs, but the live run mapped v1 to v2 before OBL-12
existed, so the prompts differed. Rewritten to call `pipeline.run_version` for each
pair, create OBL-12 between them exactly as `make live` does, and read the metrics back
out of the event log. The eval now measures the thing that ships.

**Two metric definitions that were wrong before they were useful.** Extraction
precision counted claims from the eleven changes gold does not judge, scoring the model
against rows that do not exist; it is now scoped to gold-judged changes. And the
escalation table printed three identical rows because every link scores 0.93 or above,
which looks like a broken metric; the report now prints the confidence range and says
the escalations come from the other rules rather than from confidence.

**Iteration results.**

| | extract/v1 | extract/v2 | extract/v3 |
| --- | --- | --- | --- |
| Materiality | 15/16 | 15/16 | **16/16** |
| Extraction precision | 78% | 82% | **88%** |
| Extraction recall | 88% | 88% | **94%** |
| Mapping precision | 83% | 83% | **100%** |
| Citation rejections | 1 | 1 | 1 |
| Claims | 35 | 33 | 31 |

v2 added the rule to quote the previous version where the changed text only exists
there. v3 added the rule that a paragraph summarizing or cross-referencing a duty
imposed elsewhere is not itself an obligation change, which is gold's CH-5 trap; that
one paragraph also produced the single spurious mapping link, so mapping precision went
to 100% with it.

**Stopped at two of the three planned iterations.** The third was "only if needed, one
claim per obligation altered", and the evidence says it is not needed and would not
help. CH-7 already splits correctly into two `modified` claims, one per obligation, so
the rule is already satisfied where it applies. The two remaining disagreements are not
claim-splitting failures: CH-16 produces an extra `created` claim for the queue data
that v3 folds into the report, which is defensible reading of a paragraph gold itself
describes as absorbing queue data; and CH-19 labels an obligation `removed` while
correctly calling it not material, so it routes nowhere. A rule encouraging more claims
per paragraph would risk the 16/16 materiality score to chase two labels that change no
behaviour.

**The one remaining citation rejection is correct behaviour.** `c:v2->v3:p7` is the
pure word deletion; the model names `v3:p7`, whose changed-span list is empty, so the
verifier rejects and routes to the expert queue. The v2 prompt rule did not move it.
That is the system doing what it is for: an unverifiable citation reaches a human
rather than an owner. A rejection rate reported rather than hidden is the PRD 6 metric.

## Task 15: app and templates

**Four templates, not three.** TDD 1.2 listed `review.html`, `queue.html` and
`audit.html`. A `base.html` layout holds the nav and the stylesheet so the three pages
do not carry three copies of them. TDD 1.2 and TDD 6 updated to say three page
templates over one shared layout.

**Every page is rendered from a replay, not from a cache of the last run.** The route
handlers call `events.replay` and `graph.load(conn, events.effective(...))` on each
request, so the screen and the audit trail cannot disagree, and a rollback changes what
the review page shows without any other code path being involved. At this size a
replay is milliseconds; TDD 3.9 already accepted that trade.

**The edit form is the override path.** `POST /tasks/{id}/edit` looks the change up by
the task's `change_id`, computes both keys from task 11, and writes them into the
`task_edited` payload. Without that the correction would be recorded but would never be
found again, and R4.4 would silently not hold.
`test_edit_records_an_override_with_both_keys` asserts both keys are present and that
`state.overrides` is non-empty afterwards.

**Finding: the cached v3 run depends on the text an expert types.** Loading v3 through
the app missed the cache at first. The v3 mapping prompts list OBL-12 with its name and
text, which only exist once an expert has created the node, so the recorded responses
are tied to the exact wording `make live` used. The test now walks the real journey,
resolving the new-obligation item with `pipeline.OBL_12`'s values before loading v3, and
the docstring says why.

This is a genuine prototype limitation rather than a test artefact: a reviewer who
types a different name into the create-obligation form and then loads v3 without an API
key gets a `CacheMiss` naming the call. It belongs in the README's known limitations.
The fix in a real system is that the mapping prompt would be built from a retrieval
step over a live obligation store rather than replayed from a frozen cache.

**make run is now reset, bootstrap, serve.** `pipeline bootstrap` ingests and runs
v1 to v2 from cache before uvicorn starts, so the review page has 14 changes, 9 owner
tasks and 2 expert items on first load rather than being empty.

---

## Build log summary

Sixteen tasks, one commit each plus fixes. What was generated and what was rewritten:

**Written straight from the spec and kept**: `models.py`, `db.py`, `ingest.py`,
`llm.py`, `graph.py`, `events.py`, `routing.py`, the templates. These follow TDD
Section 2 and 3 closely enough that the first implementation passed its tests.

**Rewritten during the build**:

- `diff.py`'s alignment. tasks.md said align by paragraph number, then by similarity at
  0.8. Both halves were wrong for this data: an insertion at `v2:p17` makes number
  alignment pair unrelated text from p17 onward, and a 0.8 floor applied to
  equal-length pairs rejects CH-17 (0.792) and CH-19 (0.218), which gold requires to be
  modifications. Replaced with difflib over the paragraph sequence, positional pairing
  inside equal blocks, similarity only where one side must drop out, threshold 0.6.
- `verify.py`'s step order. TDD 3.4 listed the paragraph check fourth while its prose
  said it runs first. It runs first, in code and now in the document.
- `verify.find_quote` was extracted in task 9 so mapping could check a rationale quote
  with the same search, rather than growing a second implementation of the one thing
  the product's credibility rests on.
- `evals/run_evals.py` was rewritten before its first run: the first version
  re-implemented the pipeline sequence and immediately diverged from it, using one
  obligation list for both version pairs when the live run had mapped v1 to v2 before
  OBL-12 existed. It now drives `pipeline.run_version` and reads metrics out of the log.

**Bugs found by tests, not by reading**:

- `impact_found` events were written as `impact.__dict__`, which has no `claim_id`,
  while `replay` keys impacts on `claim_id`. Every replay after a run raised `KeyError`.
  Found by a test that replays rather than trusting the return value.
- Gold VF-8 was vacuous: its `source_text_override` was byte-identical to its quote and
  the version files hold no non-ASCII characters, so the row passed whether or not
  normalization existed. Fixed with approval.
- A pipeline test stub matched on text that also appears in a neighbouring change's
  context paragraphs, and `extract.parse_response` rejected the resulting claim. The
  parser caught a malformed test.

**Three edits to `data/`, each with approval and each recorded above**: the VF-8
normalization fix, P-8 regulatory counsel, and gold IM-5 after the OBL-12 edge was
added.

**Two prompt iterations, from three planned.** The third was conditional on being
needed and was not; the reasoning is in the task 14 section.

**What the final run scores**: materiality 16/16, draft/final 2/2 by majority vote,
extraction precision 88% and recall 94%, mapping precision and recall 100%, impact
coverage 13/13 with zero leakage, citation fidelity 30 verified, 0 near, 1 rejected.
The single rejection is the system working: an unverifiable citation reaching a human
instead of an owner.

## Task 16: fresh-clone check

Cloned into an empty directory and followed README.md only, with no API key in the
environment.

| Command | Result |
| --- | --- |
| `make setup` | venv created, 33 packages installed |
| `make test` | 257 passed, no key, no network |
| `make eval` | ran, and `git status` stayed clean afterwards |
| `make run` | bootstrap processed v1 to v2, all four routes returned 200 |

Nothing had to be fixed. Two things the check confirmed that a local run cannot:

`make eval` regenerated `evals/results.md` byte for byte, so the committed table is
reproducible from the committed cache rather than an artefact of the machine that
recorded it. A clean `git status` after an eval run is the cheapest possible proof of
that, and is worth keeping as the check.

The clone contains no `.env`, no `strata.db` and no `.venv`, so the gitignore from task
1 held for the whole build. `.env.example` is the only environment file present, which
is what a reviewer needs to know a key is optional.

## Post-build: duplicate tasks across a change's claims

**Found by reading WALKTHROUGH.md against the code**, after the build was finished and
pushed. The document claimed `c:v1->v2:p12` returns one claim. It returns two, one per
obligation altered, which is what PRD R3.4 asks for and what gold CH-7 expects. Checking
that sentence turned up a real defect behind it.

`routing.create_tasks` is called per claim, so the dedupe added in task 10 only ever saw
one claim's impacts. CH-7's two claims each reached PRJ-1, DOC-1 and DOC-4 through their
own obligation, and each produced its own task:

```
c:v1->v2:p12:k1:PRJ-1   paths=[[OBL-3, PRJ-1]]
c:v1->v2:p12:k2:PRJ-1   paths=[[OBL-4, PRJ-1]]
```

Luis Ortega saw Riverside twice for one paragraph change. Gold IM-2 says this in as many
words: "PRJ-1 and DOC-1 reached twice across IM-1 and IM-2 should produce one task each,
not duplicates."

**The lesson: the tests passed on a shape the data never produces.** Both dedupe tests,
in task 10 and task 12, built a *single* claim carrying two links. That exercises the
within-claim merge, which worked, and says nothing about the case that actually occurs.
The task 12 test even used a stub whose mapping response returned both obligations for
one claim, so the stub encoded the wrong shape and then confirmed it. Neither test was
wrong about its assertion; both were wrong about the input.

Both are rewritten. `tests/test_routing.py` now builds one claim per obligation and
merges, with a docstring saying why. `tests/test_pipeline.py` drives the real cache
instead of a stub for this case, asserts CH-7 yields exactly two claims, and then asserts
one task per node. A stub that can encode a shape the pipeline cannot produce is worth
less than the recorded responses, wherever the recorded responses will do.

**The fix.** `routing.merge_tasks` merges a change's owner tasks by node: the task id
becomes `<change_id>:<node_id>`, `paths` and `obligation_ids` union, and a new
`claim_ids` field records every claim behind the task so `events._accept` accepts all of
them on approval. Expert tasks are not merged, because each carries its own claim's
escalation reason. `pipeline.run_version` now collects a change's tasks across its
claims, merges, and only then appends `task_created`, so the log holds merged tasks and a
replay of an older log is unaffected.

The review page follows: tasks hang off the change, not the claim, because a merged task
has no single owning claim. Claims are shown above as the evidence, tasks below as the
work.

Eval scores are unchanged, which is expected: the metrics measure extraction,
verification, mapping and impacts, none of which this touches. v1 to v2 goes from 11
tasks to 8.
