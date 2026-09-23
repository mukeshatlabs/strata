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
