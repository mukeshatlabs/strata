"""Version and company graph ingestion (PRD R1.1, R1.2, R1.3)."""

import json

import pytest

from strata import db, ingest

V1 = "data/proceeding/v1.md"
V2 = "data/proceeding/v2.md"
V3 = "data/proceeding/v3.md"
COMPANY = "data/company/meridian.json"

SECTIONS_V1 = [
    "Caption",
    "Background",
    "Summary of Proposed Rule",
    "Ordering Paragraphs",
    "Compliance and Reporting",
    "Procedural Matters",
    "Notes",
]


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    yield c
    c.close()


def paragraphs(conn, version_id):
    return conn.execute(
        "select * from paragraphs where version_id=? order by number", (version_id,)
    ).fetchall()


def test_header_fields_parsed(conn):
    v = ingest.ingest_version(conn, V1)
    assert v.version_id == "v1"
    assert v.number == 1
    assert v.docket == "26-0412-RM"
    assert v.status == "proposed"
    assert v.issued == "2026-03-10"
    assert v.effective is None
    assert v.title.startswith("Rulemaking to Establish Interconnection Procedures")


def test_final_version_has_effective_date(conn):
    v = ingest.ingest_version(conn, V3)
    assert (v.version_id, v.status, v.effective) == ("v3", "final", "2027-01-01")


def test_v1_yields_26_paragraphs_with_sequential_ids(conn):
    ingest.ingest_version(conn, V1)
    rows = paragraphs(conn, "v1")
    assert len(rows) == 26
    assert [r["para_id"] for r in rows] == [f"v1:p{n}" for n in range(1, 27)]
    assert [r["number"] for r in rows] == list(range(1, 27))


def test_paragraph_counts_for_every_version(conn):
    for path, version_id, count in ((V1, "v1", 26), (V2, "v2", 26), (V3, "v3", 25)):
        ingest.ingest_version(conn, path)
        assert len(paragraphs(conn, version_id)) == count


def test_sections_are_assigned(conn):
    ingest.ingest_version(conn, V1)
    rows = paragraphs(conn, "v1")
    assert [r["section"] for r in rows].count("Caption") == 1
    assert list(dict.fromkeys(r["section"] for r in rows)) == SECTIONS_V1
    by_id = {r["para_id"]: r for r in rows}
    assert by_id["v1:p1"]["section"] == "Caption"
    assert by_id["v1:p24"]["section"] == "Notes"


def test_char_offsets_index_the_file_exactly(conn):
    """text == file[char_start:char_end] for every paragraph of every version."""
    for path, version_id in ((V1, "v1"), (V2, "v2"), (V3, "v3")):
        ingest.ingest_version(conn, path)
        source = open(path, encoding="utf-8").read()
        for r in paragraphs(conn, version_id):
            assert source[r["char_start"] : r["char_end"]] == r["text"], r["para_id"]


def test_char_start_points_past_the_marker(conn):
    ingest.ingest_version(conn, V1)
    source = open(V1, encoding="utf-8").read()
    row = paragraphs(conn, "v1")[0]
    assert source[row["char_start"] - 4 : row["char_start"]] == "[1] "
    assert not row["text"].startswith("[1]")


def test_footnote_marker_is_kept_in_text(conn):
    """The [n] paragraph marker is stripped; an inline [^1] footnote marker is not."""
    ingest.ingest_version(conn, V1)
    row = conn.execute("select * from paragraphs where para_id='v1:p24'").fetchone()
    assert row["text"].startswith("[^1] Docket No. 21-0187-RM")


def test_version_row_written(conn):
    ingest.ingest_version(conn, V2)
    row = conn.execute("select * from versions where version_id='v2'").fetchone()
    assert row["status"] == "revised_proposed"
    assert row["company_id"] == "meridian"
    assert row["path"].endswith("v2.md")


def test_company_graph_loads(conn):
    nodes, edges = ingest.ingest_company(conn, COMPANY)
    assert len(nodes) == 31
    assert len(edges) == 21
    assert conn.execute("select count(*) from nodes").fetchone()[0] == 31
    assert conn.execute("select count(*) from edges").fetchone()[0] == 21


def test_node_types_and_attrs(conn):
    ingest.ingest_company(conn, COMPANY)
    counts = {
        r["type"]: r["n"]
        for r in conn.execute("select type, count(*) as n from nodes group by type")
    }
    assert counts == {"obligation": 11, "project": 5, "document": 8, "person": 7}

    obl = conn.execute("select * from nodes where node_id='OBL-3'").fetchone()
    assert obl["owner"] == "P-2"
    assert json.loads(obl["attrs"])["source_para"] == "v1:p12"
    assert json.loads(obl["attrs"])["docket"] == "26-0412-RM"

    prj = conn.execute("select * from nodes where node_id='PRJ-1'").fetchone()
    assert json.loads(prj["attrs"])["status"] == "active"

    person = conn.execute("select * from nodes where node_id='P-1'").fetchone()
    assert person["owner"] is None
    assert json.loads(person["attrs"]) == {}


def test_edges_loaded_with_types(conn):
    ingest.ingest_company(conn, COMPANY)
    row = conn.execute(
        "select * from edges where from_id='PRJ-1' and to_id='OBL-3'"
    ).fetchone()
    assert row["type"] == "depends_on"
    counts = {
        r["type"]: r["n"]
        for r in conn.execute("select type, count(*) as n from edges group by type")
    }
    assert counts == {"implements": 13, "depends_on": 5, "references": 3}


def test_reingest_version_is_a_no_op(conn):
    ingest.ingest_version(conn, V1)
    before = [tuple(r) for r in paragraphs(conn, "v1")]
    ingest.ingest_version(conn, V1)
    after = [tuple(r) for r in paragraphs(conn, "v1")]
    assert before == after
    assert conn.execute("select count(*) from versions").fetchone()[0] == 1


def test_reingest_company_is_a_no_op(conn):
    ingest.ingest_company(conn, COMPANY)
    ingest.ingest_company(conn, COMPANY)
    assert conn.execute("select count(*) from nodes").fetchone()[0] == 31
    assert conn.execute("select count(*) from edges").fetchone()[0] == 21


def test_dangling_edge_raises_naming_the_edge(conn, tmp_path):
    bad = {
        "company_id": "meridian",
        "nodes": [{"id": "OBL-1", "type": "obligation", "name": "x", "text": "y"}],
        "edges": [{"from": "PRJ-99", "to": "OBL-1", "type": "depends_on"}],
    }
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError) as err:
        ingest.ingest_company(conn, path)
    assert "PRJ-99" in str(err.value)
    assert conn.execute("select count(*) from edges").fetchone()[0] == 0


def test_body_line_without_a_marker_raises(conn, tmp_path):
    path = tmp_path / "bad.md"
    path.write_text(
        "---\ndocket: D\ntitle: T\nversion: 1\nstatus: proposed\n"
        "issued: 2026-01-01\neffective:\n---\n\n## Caption\n\n"
        "[1] A numbered paragraph.\n\nA loose line with no marker.\n"
    )
    with pytest.raises(ValueError) as err:
        ingest.ingest_version(conn, path)
    assert "loose line" in str(err.value).lower() or "marker" in str(err.value).lower()
