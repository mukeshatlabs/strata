"""Parse version files and the company graph into the database (PRD R1.1-R1.3).

A version file is a `---` header block followed by `## Section` headings and
`[n]` paragraphs. Paragraph text excludes the `[n] ` marker, and char_start is
the file offset of the first character of that text, so a paragraph-local
offset p is file offset char_start + p with no correction (TDD 2.1).
"""

import json
import re
from pathlib import Path

from .models import COMPANY_ID, Edge, Node, Paragraph, Version

HEADER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
SECTION_RE = re.compile(r"^##\s+(.*?)\s*$")
PARA_RE = re.compile(r"^\[(\d+)\]\s+")
NODE_FIELDS = {"id", "type", "name", "text", "owner"}


def _parse_header(text: str, path: Path) -> dict:
    """Return the `key: value` pairs of the leading `---` block."""
    match = HEADER_RE.match(text)
    if not match:
        raise ValueError(f"{path}: no --- header block at the top of the file")
    header = {}
    for line in match.group(1).split("\n"):
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"{path}: header line is not 'key: value': {line!r}")
        header[key.strip()] = value.strip()
    return header


def _parse_paragraphs(text: str, version_id: str, body_start: int, path: Path
                      ) -> list[Paragraph]:
    """Walk the body, tracking the current section, and emit one Paragraph per [n]."""
    paragraphs = []
    section = None
    offset = body_start
    for line in text[body_start:].split("\n"):
        stripped = line.strip()
        if not stripped:
            offset += len(line) + 1
            continue
        heading = SECTION_RE.match(stripped)
        if heading:
            section = heading.group(1)
            offset += len(line) + 1
            continue
        marker = PARA_RE.match(stripped)
        if not marker:
            raise ValueError(
                f"{path}: body line has no [n] paragraph marker: {stripped[:60]!r}"
            )
        number = int(marker.group(1))
        lead = line.index("[") + marker.end()
        body = line[lead:]
        paragraphs.append(
            Paragraph(
                para_id=f"{version_id}:p{number}",
                version_id=version_id,
                number=number,
                section=section,
                text=body,
                char_start=offset + lead,
                char_end=offset + lead + len(body),
            )
        )
        offset += len(line) + 1
    return paragraphs


def ingest_version(conn, path) -> Version:
    """Parse a version file into its Version row and Paragraph rows. Idempotent."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    header = _parse_header(text, path)
    number = int(header["version"])
    version = Version(
        version_id=f"v{number}",
        docket=header["docket"],
        title=header["title"],
        number=number,
        status=header["status"],
        issued=header.get("issued") or None,
        effective=header.get("effective") or None,
        path=str(path),
    )
    body_start = HEADER_RE.match(text).end()
    paragraphs = _parse_paragraphs(text, version.version_id, body_start, path)

    conn.execute(
        "insert or replace into versions (company_id, version_id, docket, title,"
        " number, status, issued, effective, path) values (?,?,?,?,?,?,?,?,?)",
        (version.company_id, version.version_id, version.docket, version.title,
         version.number, version.status, version.issued, version.effective,
         version.path),
    )
    conn.executemany(
        "insert or replace into paragraphs (company_id, para_id, version_id, number,"
        " section, text, char_start, char_end) values (?,?,?,?,?,?,?,?)",
        [(p.company_id, p.para_id, p.version_id, p.number, p.section, p.text,
          p.char_start, p.char_end) for p in paragraphs],
    )
    conn.commit()
    return version


def ingest_company(conn, path) -> tuple[list[Node], list[Edge]]:
    """Load the company graph JSON into the nodes and edges tables. Idempotent."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    company_id = data.get("company_id", COMPANY_ID)

    nodes = [
        Node(
            node_id=raw["id"],
            type=raw["type"],
            name=raw["name"],
            text=raw.get("text", ""),
            owner=raw.get("owner"),
            attrs={k: v for k, v in raw.items() if k not in NODE_FIELDS and v is not None},
            company_id=company_id,
        )
        for raw in data["nodes"]
    ]

    known = {n.node_id for n in nodes}
    edges = []
    for raw in data["edges"]:
        for end in ("from", "to"):
            if raw[end] not in known:
                raise ValueError(
                    f"{path}: edge {raw['from']} -{raw['type']}-> {raw['to']} refers to"
                    f" unknown node {raw[end]!r}"
                )
        edges.append(
            Edge(from_id=raw["from"], to_id=raw["to"], type=raw["type"],
                 company_id=company_id)
        )

    conn.executemany(
        "insert or replace into nodes (company_id, node_id, type, name, text, owner,"
        " attrs) values (?,?,?,?,?,?,?)",
        [(n.company_id, n.node_id, n.type, n.name, n.text, n.owner,
          json.dumps(n.attrs)) for n in nodes],
    )
    conn.executemany(
        "insert or replace into edges (company_id, from_id, to_id, type)"
        " values (?,?,?,?)",
        [(e.company_id, e.from_id, e.to_id, e.type) for e in edges],
    )
    conn.commit()
    return nodes, edges
