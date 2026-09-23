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
