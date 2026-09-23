"""Citation verification (TDD 3.4). No model is involved.

The verifier is the reason the product is trustworthy, so it is deliberately
mechanical: normalize, check the paragraph the claim names, search, then check
that the match overlaps the text the diff says changed. Everything it reports is
reproducible from the source files alone.

Offsets: matching happens in normalized coordinates, but `match_start` and
`match_end` are reported in the original paragraph's coordinates, so they are
directly comparable to `Change.spans_*` and can be used to highlight the source.
"""

from rapidfuzz.distance import Levenshtein

from .models import Verification

SUBSTITUTIONS = {
    "‘": "'", "’": "'",      # curly single quotes
    "“": '"', "”": '"',      # curly double quotes
    "–": "-", "—": "-",      # en dash, em dash
    "−": "-",                     # minus sign
    " ": " ", " ": " ",      # non-breaking and thin space
}


def normalize(text: str) -> tuple[str, list[int]]:
    """Return normalized text and a map from each normalized index to the original.

    Quotes, dashes and exotic spaces are unified one for one; runs of whitespace
    collapse to a single space, emitted at the index of the run's first
    character. The map has one entry per normalized character plus a final entry
    holding len(text), so a normalized span [s, e) maps to [map[s], map[e]).
    """
    out: list[str] = []
    offsets: list[int] = []
    previous_was_space = False
    for index, char in enumerate(text):
        char = SUBSTITUTIONS.get(char, char)
        if char.isspace():
            if previous_was_space:
                continue
            out.append(" ")
            offsets.append(index)
            previous_was_space = True
            continue
        out.append(char)
        offsets.append(index)
        previous_was_space = False
    offsets.append(len(text))
    return "".join(out), offsets


def threshold(quote: str) -> int:
    """Edit distance allowed for a near match: max(3, len // 25) (TDD 3.4)."""
    return max(3, len(quote) // 25)


def _best_window(haystack: str, needle: str, limit: int) -> tuple[int, int] | None:
    """Return (offset, distance) of the closest window, or None if none is within limit."""
    width = len(needle)
    if not width or width > len(haystack) + limit:
        return None
    best: tuple[int, int] | None = None
    for start in range(0, max(1, len(haystack) - width + 1 + limit)):
        window = haystack[start : start + width]
        distance = Levenshtein.distance(needle, window, score_cutoff=limit)
        if distance <= limit and (best is None or distance < best[1]):
            best = (start, distance)
            if distance == 0:
                break
    return best


def _quotable(change) -> dict[str, list]:
    """Map each paragraph the claim may quote to the spans that changed in it.

    A modification may be quoted from either side: the changed text of a pure
    deletion exists only in the from-version. An addition has only a to-side and
    a removal only a from-side.
    """
    sides = {}
    if change.para_id_to:
        sides[change.para_id_to] = change.spans_to
    if change.para_id_from:
        sides[change.para_id_from] = change.spans_from
    return sides


def _overlaps(start: int, end: int, spans: list) -> bool:
    """True if [start, end) intersects any span."""
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def find_quote(quote: str, text: str) -> tuple[str, int | None, int | None, int | None]:
    """Search for a quote in a passage, with no change record involved.

    Returns (status, start, end, distance) with offsets in the original text.
    Status is verified on an exact find, near within the length-scaled
    threshold, rejected otherwise. This is the search half of verify_claim, and
    mapping.py uses it to check a rationale quote against an obligation's text.
    """
    source, offsets = normalize(text)
    needle, _ = normalize(quote)
    if not needle:
        return "rejected", None, None, None

    position = source.find(needle)
    if position >= 0:
        start, end, distance, status = position, position + len(needle), 0, "verified"
    else:
        best = _best_window(source, needle, threshold(needle))
        if best is None:
            return "rejected", None, None, None
        start, distance = best
        end = min(start + len(needle), len(source))
        status = "near"
    return status, offsets[start], offsets[min(end, len(source))], distance


def verify_claim(claim, paragraphs: dict, change) -> Verification:
    """Verify one claim's quote against the source text (TDD 3.4).

    Returns a Verification with status verified, near, or rejected. A rejection
    carries one of wrong_paragraph, quote_outside_changed_span, quote_not_found.
    """
    sides = _quotable(change)

    # 1. The paragraph check runs before any search, so a real quote taken from a
    #    neighbouring paragraph is caught as wrong_paragraph and not as not-found.
    if claim.quote_para_id not in sides or claim.quote_para_id not in paragraphs:
        return Verification(
            claim_id=claim.claim_id, status="rejected", reason="wrong_paragraph"
        )

    spans = sides[claim.quote_para_id]

    # 2 and 3. Normalize, exact find, then sliding-window Levenshtein.
    status, match_start, match_end, distance = find_quote(
        claim.quote, paragraphs[claim.quote_para_id]
    )
    if status == "rejected":
        return Verification(
            claim_id=claim.claim_id, status="rejected", reason="quote_not_found"
        )

    # 4. The match must overlap the span the diff identified as changed.
    if not _overlaps(match_start, match_end, spans):
        return Verification(
            claim_id=claim.claim_id,
            status="rejected",
            match_start=match_start,
            match_end=match_end,
            edit_distance=distance,
            overlaps_change=False,
            reason="quote_outside_changed_span",
        )

    return Verification(
        claim_id=claim.claim_id,
        status=status,
        match_start=match_start,
        match_end=match_end,
        edit_distance=distance,
        overlaps_change=True,
    )
