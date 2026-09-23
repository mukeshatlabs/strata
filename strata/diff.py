"""Paragraph alignment and word-level change spans (PRD R2.1). No model.

Alignment runs difflib over the sequence of paragraph texts, so a paragraph that
only moved because of an insertion or removal comes back as `equal` and emits
nothing. Inside a `replace` block of equal length, paragraphs pair by position
with no similarity floor, because a paragraph can be replaced wholesale and is
still a modification (gold CH-17 at 0.79, CH-19 at 0.22). Only an unequal block,
where one side has to drop out, is decided by similarity.

Every SequenceMatcher here passes autojunk=False. The default heuristic treats
common characters in sequences over 200 elements as junk, which drops the
CH-16 pairing from 0.80 to 0.35 and breaks the alignment.
"""

import difflib
import re

from .models import Change

SIMILARITY_THRESHOLD = 0.6
WORD_RE = re.compile(r"\S+\s*")


def _ratio(a: str, b: str) -> float:
    """Character-level similarity of two paragraphs, 0 to 1."""
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _load(conn, version_id: str) -> list[tuple[str, str]]:
    """Return (para_id, text) for one version, in paragraph order."""
    return [
        (r["para_id"], r["text"])
        for r in conn.execute(
            "select para_id, text from paragraphs where version_id=? order by number",
            (version_id,),
        )
    ]


def _word_spans(text_from: str, text_to: str) -> tuple[list, list]:
    """Word-level diff of two paragraphs, as character spans in each."""
    a = WORD_RE.findall(text_from)
    b = WORD_RE.findall(text_to)
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    spans_from, spans_to = [], []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        start = len("".join(a[:i1]))
        end = start + len("".join(a[i1:i2]))
        if end > start:
            spans_from.append((start, end))
        start = len("".join(b[:j1]))
        end = start + len("".join(b[j1:j2]))
        if end > start:
            spans_to.append((start, end))
    return spans_from, spans_to


def _pair_block(block_from: list, block_to: list) -> list[tuple]:
    """Pair the paragraphs of one replace block, returning (from, to) with None gaps.

    Equal-length blocks pair by position. Unequal blocks take the highest-ratio
    pairs that preserve order and clear SIMILARITY_THRESHOLD; whatever is left
    over on either side becomes a removal or an addition.
    """
    if len(block_from) == len(block_to):
        return list(zip(block_from, block_to))

    candidates = sorted(
        (
            (_ratio(text_from, text_to), i, j)
            for i, (_, text_from) in enumerate(block_from)
            for j, (_, text_to) in enumerate(block_to)
        ),
        key=lambda c: (-c[0], c[1], c[2]),
    )
    pairs: dict[int, int] = {}
    for ratio, i, j in candidates:
        if ratio < SIMILARITY_THRESHOLD or i in pairs or j in pairs.values():
            continue
        if any((i - k) * (j - v) < 0 for k, v in pairs.items()):
            continue  # would cross an existing pair
        pairs[i] = j

    result = []
    for i, item in enumerate(block_from):
        result.append((item, block_to[pairs[i]] if i in pairs else None))
    for j, item in enumerate(block_to):
        if j not in pairs.values():
            result.append((None, item))
    return result


def _make_change(from_version, to_version, item_from, item_to) -> Change:
    """Build one Change from an aligned pair, either side of which may be None."""
    para_from, text_from = item_from or (None, "")
    para_to, text_to = item_to or (None, "")
    if para_from and para_to:
        kind, number = "modified", para_from.split(":p")[1]
        marker = ""
    elif para_to:
        kind, number, marker = "added", para_to.split(":p")[1], "+"
    else:
        kind, number, marker = "removed", para_from.split(":p")[1], "-"
    spans_from, spans_to = (
        _word_spans(text_from, text_to)
        if kind == "modified"
        else ([(0, len(text_from))] if text_from else [],
              [(0, len(text_to))] if text_to else [])
    )
    return Change(
        change_id=f"c:{from_version}->{to_version}:{marker}p{number}",
        from_version=from_version,
        to_version=to_version,
        para_id_from=para_from,
        para_id_to=para_to,
        kind=kind,
        spans_from=spans_from,
        spans_to=spans_to,
        text_from=text_from,
        text_to=text_to,
    )


def diff_versions(conn, from_id: str, to_id: str) -> list[Change]:
    """Diff two ingested versions into Change records, in to-version order."""
    block_from = _load(conn, from_id)
    block_to = _load(conn, to_id)
    matcher = difflib.SequenceMatcher(
        None, [t for _, t in block_from], [t for _, t in block_to], autojunk=False
    )
    changes = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "delete":
            pairs = [(item, None) for item in block_from[i1:i2]]
        elif tag == "insert":
            pairs = [(None, item) for item in block_to[j1:j2]]
        else:
            pairs = _pair_block(block_from[i1:i2], block_to[j1:j2])
        changes.extend(
            _make_change(from_id, to_id, item_from, item_to)
            for item_from, item_to in pairs
        )
    return changes
