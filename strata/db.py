"""SQLite connection and schema.

Five tables: versions, paragraphs, nodes, edges, events. Every table carries a
company_id so a second company is a data change and not a schema change (TDD 7).
Writes live in ingest.py and events.py, not here.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
create table if not exists versions (
    company_id text not null,
    version_id text not null,
    docket     text not null,
    title      text not null,
    number     integer not null,
    status     text not null,
    issued     text,
    effective  text,
    path       text,
    primary key (company_id, version_id)
);

create table if not exists paragraphs (
    company_id text not null,
    para_id    text not null,
    version_id text not null,
    number     integer not null,
    section    text,
    text       text not null,
    char_start integer not null,
    char_end   integer not null,
    primary key (company_id, para_id)
);

create table if not exists nodes (
    company_id text not null,
    node_id    text not null,
    type       text not null,
    name       text not null,
    text       text,
    owner      text,
    attrs      text not null default '{}',
    primary key (company_id, node_id)
);

create table if not exists edges (
    company_id text not null,
    from_id    text not null,
    to_id      text not null,
    type       text not null,
    primary key (company_id, from_id, to_id, type)
);

create table if not exists events (
    seq        integer primary key autoincrement,
    company_id text not null,
    project_id text not null,
    ts         text not null,
    actor      text not null,
    type       text not null,
    subject_id text,
    payload    text not null default '{}'
);

create index if not exists idx_paragraphs_version on paragraphs (company_id, version_id);
create index if not exists idx_events_project on events (company_id, project_id, seq);
create index if not exists idx_nodes_type on nodes (company_id, type);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the database at path (or ':memory:') and return a Row-factory connection."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create every table and index if absent. Safe to call on an existing database."""
    conn.executescript(SCHEMA)
    conn.commit()
