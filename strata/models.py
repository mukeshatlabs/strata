"""Record types for every stage of the pipeline.

One dataclass per record in TDD Section 2. Versions, paragraphs, nodes, edges,
and events are stored in SQLite; the rest are in-memory records that reach
durable storage as event payloads, because project state is derived by replay
and never written directly (TDD 2.5).
"""

from dataclasses import dataclass, field

COMPANY_ID = "meridian"

Span = tuple[int, int]


@dataclass(frozen=True)
class Version:
    """One version of a proceeding, parsed from a file's header block."""

    version_id: str
    docket: str
    title: str
    number: int
    status: str
    issued: str
    effective: str | None
    path: str
    company_id: str = COMPANY_ID


@dataclass(frozen=True)
class Paragraph:
    """One numbered paragraph, addressable by ID and character span (PRD R1.3)."""

    para_id: str
    version_id: str
    number: int
    section: str
    text: str
    char_start: int
    char_end: int
    company_id: str = COMPANY_ID


@dataclass(frozen=True)
class Node:
    """A company graph node: obligation, project, document, or person.

    Type-specific fields (source_para and docket on obligations, status on
    projects) live in attrs so one table covers all four types.
    """

    node_id: str
    type: str
    name: str
    text: str
    owner: str | None = None
    attrs: dict = field(default_factory=dict)
    company_id: str = COMPANY_ID


@dataclass(frozen=True)
class Edge:
    """A typed relationship: depends_on, implements, or references."""

    from_id: str
    to_id: str
    type: str
    company_id: str = COMPANY_ID


@dataclass(frozen=True)
class Change:
    """One paragraph that differs between two versions, with changed spans."""

    change_id: str
    from_version: str
    to_version: str
    para_id_from: str | None
    para_id_to: str | None
    kind: str
    spans_from: list[Span] = field(default_factory=list)
    spans_to: list[Span] = field(default_factory=list)
    text_from: str = ""
    text_to: str = ""


@dataclass(frozen=True)
class Claim:
    """A model judgment about one change, carrying a quote that must verify."""

    claim_id: str
    change_id: str
    material: bool
    version_status: str
    obligation_change: str
    summary: str
    quote: str
    quote_para_id: str
    model_confidence: float


@dataclass(frozen=True)
class Verification:
    """The verifier's finding on one claim's quote (TDD 3.4). No model."""

    claim_id: str
    status: str
    match_start: int | None = None
    match_end: int | None = None
    edit_distance: int | None = None
    overlaps_change: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class Link:
    """A proposed link from a claim to the obligation it modifies.

    rationale_status holds the verification of rationale_quote against the
    obligation's own text; it is None until mapping fills it in.
    """

    claim_id: str
    obligation_id: str
    confidence: float
    rationale: str
    rationale_quote: str = ""
    rationale_status: str | None = None


@dataclass(frozen=True)
class Impact:
    """A node reached from a modified obligation, with the path that reached it."""

    obligation_id: str
    node_id: str
    node_type: str
    path: list[str]
    edge_types: list[str]
    hops: int
    owner: str | None = None


@dataclass(frozen=True)
class ReviewTask:
    """One item in the review center, in the owner queue or the expert queue."""

    task_id: str
    project_id: str
    claim_id: str
    change_id: str
    node_id: str | None
    queue: str
    assignee: str | None
    recommended_action: str
    status: str = "open"
    reason: str | None = None


@dataclass(frozen=True)
class Event:
    """One append-only log row. seq is assigned by the database on insert."""

    seq: int | None
    ts: str
    actor: str
    type: str
    subject_id: str
    payload: dict = field(default_factory=dict)
    project_id: str = "proj-1"
    company_id: str = COMPANY_ID
