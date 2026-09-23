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
