"""Schema creation and one round-trip per stored record type (PRD R1.3)."""

import json
import sqlite3

import pytest

from strata import db
from strata.models import Edge, Event, Node, Paragraph, Version

TABLES = {"versions", "paragraphs", "nodes", "edges", "events"}


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    yield c
    c.close()


def test_connect_returns_rows_by_name(tmp_path):
    c = db.connect(tmp_path / "strata.db")
    db.init_schema(c)
    row = c.execute("select 1 as n").fetchone()
    assert row["n"] == 1
    c.close()


def test_init_schema_creates_every_table(conn):
    names = {
        r["name"]
        for r in conn.execute("select name from sqlite_master where type='table'")
    }
    assert TABLES <= names


def test_init_schema_is_idempotent(conn):
    db.init_schema(conn)
    db.init_schema(conn)
    names = {
        r["name"]
        for r in conn.execute("select name from sqlite_master where type='table'")
    }
    assert TABLES <= names


def test_version_round_trip(conn):
    v = Version(
        version_id="v2",
        docket="26-0412-RM",
        title="Rulemaking on Interconnection Procedures",
        number=2,
        status="revised_proposed",
        issued="2026-05-14",
        effective=None,
        path="data/proceeding/v2.md",
        company_id="meridian",
    )
    conn.execute(
        "insert into versions (company_id, version_id, docket, title, number,"
        " status, issued, effective, path) values (?,?,?,?,?,?,?,?,?)",
        (
            v.company_id,
            v.version_id,
            v.docket,
            v.title,
            v.number,
            v.status,
            v.issued,
            v.effective,
            v.path,
        ),
    )
    row = conn.execute("select * from versions where version_id='v2'").fetchone()
    assert row["docket"] == "26-0412-RM"
    assert row["number"] == 2
    assert row["effective"] is None
    assert row["company_id"] == "meridian"


def test_paragraph_round_trip(conn):
    p = Paragraph(
        para_id="v2:p12",
        version_id="v2",
        number=12,
        section="Ordering Paragraphs",
        text="Each electric utility shall complete the interconnection study.",
        char_start=1200,
        char_end=1262,
        company_id="meridian",
    )
    conn.execute(
        "insert into paragraphs (company_id, para_id, version_id, number, section,"
        " text, char_start, char_end) values (?,?,?,?,?,?,?,?)",
        (
            p.company_id,
            p.para_id,
            p.version_id,
            p.number,
            p.section,
            p.text,
            p.char_start,
            p.char_end,
        ),
    )
    row = conn.execute("select * from paragraphs where para_id='v2:p12'").fetchone()
    assert row["section"] == "Ordering Paragraphs"
    assert (row["char_start"], row["char_end"]) == (1200, 1262)


def test_node_round_trip_preserves_attrs_json(conn):
    n = Node(
        node_id="OBL-3",
        type="obligation",
        name="Study completion within 45 business days",
        text="Complete the interconnection study for a small storage resource.",
        owner="P-2",
        attrs={"source_para": "v1:p12", "docket": "26-0412-RM"},
        company_id="meridian",
    )
    conn.execute(
        "insert into nodes (company_id, node_id, type, name, text, owner, attrs)"
        " values (?,?,?,?,?,?,?)",
        (n.company_id, n.node_id, n.type, n.name, n.text, n.owner, json.dumps(n.attrs)),
    )
    row = conn.execute("select * from nodes where node_id='OBL-3'").fetchone()
    assert row["type"] == "obligation"
    assert row["owner"] == "P-2"
    assert json.loads(row["attrs"]) == {
        "source_para": "v1:p12",
        "docket": "26-0412-RM",
    }


def test_edge_round_trip(conn):
    e = Edge(from_id="PRJ-1", to_id="OBL-3", type="depends_on", company_id="meridian")
    conn.execute(
        "insert into edges (company_id, from_id, to_id, type) values (?,?,?,?)",
        (e.company_id, e.from_id, e.to_id, e.type),
    )
    row = conn.execute("select * from edges where from_id='PRJ-1'").fetchone()
    assert (row["to_id"], row["type"]) == ("OBL-3", "depends_on")


def test_event_round_trip_preserves_payload_json(conn):
    ev = Event(
        seq=None,
        ts="2026-05-14T10:00:00",
        actor="system",
        type="claim_verified",
        subject_id="c:v1->v2:p12:k1",
        payload={"status": "verified", "edit_distance": 0},
        project_id="proj-1",
        company_id="meridian",
    )
    conn.execute(
        "insert into events (company_id, project_id, ts, actor, type, subject_id,"
        " payload) values (?,?,?,?,?,?,?)",
        (
            ev.company_id,
            ev.project_id,
            ev.ts,
            ev.actor,
            ev.type,
            ev.subject_id,
            json.dumps(ev.payload),
        ),
    )
    row = conn.execute("select * from events").fetchone()
    assert row["type"] == "claim_verified"
    assert json.loads(row["payload"])["edit_distance"] == 0


def test_event_seq_autoincrements(conn):
    for i in range(3):
        conn.execute(
            "insert into events (company_id, project_id, ts, actor, type, subject_id,"
            " payload) values (?,?,?,?,?,?,?)",
            ("meridian", "proj-1", "2026-05-14T10:00:00", "system", "task_created",
             f"t-{i}", "{}"),
        )
    seqs = [r["seq"] for r in conn.execute("select seq from events order by seq")]
    assert seqs == [1, 2, 3]


def test_foreign_keys_pragma_is_on(conn):
    assert conn.execute("pragma foreign_keys").fetchone()[0] == 1


def test_dangling_edge_is_not_rejected_by_the_schema(conn):
    """Validation belongs to ingest, which can name the bad edge (Task 3)."""
    conn.execute(
        "insert into edges (company_id, from_id, to_id, type) values (?,?,?,?)",
        ("meridian", "PRJ-99", "OBL-99", "depends_on"),
    )
    assert conn.execute("select count(*) from edges").fetchone()[0] == 1


def test_unknown_table_is_absent(conn):
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("select * from claims")
