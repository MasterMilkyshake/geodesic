"""Literature search: pagination, retries, incomplete retrieval, truncation, resuming, snapshots,
synthetic/real separation and what is recorded for every run. No test touches the network."""

from __future__ import annotations

import json

import httpx
import pytest

from conftest import OPERATOR, cursor_transport
from geodesic import audit, protocol as P
from geodesic.config import ConceptGroup
from geodesic.errors import ConfigError, GeodesicError, ProvenanceError
from geodesic.provenance import TAG_TEXT
from geodesic.search import europepmc, runs, snapshot
from geodesic.synthetic import search as SS
from geodesic.synthetic.protocol import example_protocol
from geodesic.util import sha256_file

QUERY = '"dye tracer" AND conduit'


def _run(project, transport, **kwargs):
    kwargs.setdefault("query", QUERY)
    kwargs.setdefault("page_size", 4)
    return europepmc.run_search(project, OPERATOR, mode="fixture", transport=transport, sleep=lambda s: None,
                                **kwargs)


def test_pagination_follows_cursors_and_run_record_is_complete(project):
    requests: list[dict] = []
    out = _run(project, cursor_transport(SS.europepmc_pages(4), requests=requests))
    run = out["run"]
    assert [r["cursorMark"] for r in requests] == ["*", "SYNCURSOR001", "SYNCURSOR002"]
    assert all(r["query"] == QUERY and r["pageSize"] == "4" and r["format"] == "json" for r in requests)
    assert (run["status"], run["retrieved_count"], run["provider_total"], run["pages_retrieved"]) == \
        ("complete", 10, 10, 3)
    assert out["links"] == {"new_paper": 10, "exact_duplicate_record": 0, "validated_identifier_match": 0}
    # what the run record states
    assert run["provider"] == "europepmc" and run["mode"] == "fixture" and "cursorMark" in run["interface"]
    assert run["executed_query"] == QUERY and run["page_size"] == 4
    assert run["max_records"] == project.config.search.max_records
    assert run["filters"]["derivation"] == "explicit source-specific query"
    assert run["started_at"].endswith("+00:00") and run["finished_at"].endswith("+00:00")
    assert run["errors"] == [] and run["resume_cursor"] is None
    # exploratory: the protocol is provisional, and the run says so with the protocol version
    pv = P.current(project.conn)
    assert run["exploratory"] == 1 and run["protocol_status_at_run"] == "provisional"
    assert run["protocol_version_id"] == pv.id
    assert "FIXTURE" in run["label"] and "exploratory" in run["label"] and "retrieved 10 of 10" in run["label"]
    # raw responses are stored byte-for-byte with their hashes
    raws = project.conn.execute("SELECT * FROM search_raw_response WHERE search_run_id = ? ORDER BY seq",
                                (run["id"],)).fetchall()
    assert len(raws) == 3 and all(r["is_fixture"] == 1 for r in raws)
    for raw, page in zip(raws, SS.europepmc_pages(4)):
        path = project.abs(raw["stored_path"])
        assert path.read_bytes() == page and sha256_file(path) == raw["sha256"] and raw["size_bytes"] == len(page)
    # each record keeps its source form, its position and its membership of the run
    rows = project.conn.execute("SELECT position, raw_json, paper_id, link_method FROM search_result WHERE"
                                " search_run_id = ? ORDER BY position", (run["id"],)).fetchall()
    assert [r["position"] for r in rows] == list(range(10))
    assert json.loads(rows[0]["raw_json"]) == SS.records()[0]
    events = [e["action"] for e in audit.history(project.conn, entity_type="search_run", entity_id=run["id"])]
    assert events.count("search.start") == 1 and events.count("search.finish") == 1


def test_run_under_confirmed_protocol_is_not_labelled_exploratory(project, reviewer_a):
    P.save_new_version(project.conn, example_protocol(), reviewer_a, "criteria entered")
    pv = P.confirm(project.conn, reviewer_a, project.data_class)
    run = _run(project, cursor_transport(SS.europepmc_pages(4)))["run"]
    assert run["exploratory"] == 0 and run["protocol_status_at_run"] == "confirmed"
    assert run["protocol_version_id"] == pv.id and "exploratory" not in run["label"]


def test_retry_after_is_honoured_and_retries_are_recorded(project):
    sleeps: list[float] = []
    transport = cursor_transport(SS.europepmc_pages(4), failures={
        "*": [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(503)]})
    out = europepmc.run_search(project, OPERATOR, query=QUERY, page_size=4, mode="fixture", transport=transport,
                               sleep=sleeps.append)
    run = out["run"]
    assert run["status"] == "complete" and run["retrieved_count"] == 10
    assert sleeps == [7.0, project.config.search.backoff_seconds * 2]      # Retry-After, then exponential backoff
    retried = [e for e in run["errors"] if e["kind"] == "retried_request"]
    assert [(e["attempt"], e["problem"], e["retry_after"]) for e in retried] == \
        [(1, "HTTP 429", 7.0), (2, "HTTP 503", None)]


def test_retries_are_bounded_and_the_run_is_incomplete_then_resumable(project):
    sleeps: list[float] = []
    requests: list[dict] = []
    failing = cursor_transport(SS.europepmc_pages(4), requests=requests,
                               failures={"SYNCURSOR001": [httpx.Response(503), ...]})
    out = europepmc.run_search(project, OPERATOR, query=QUERY, page_size=4, mode="fixture", transport=failing,
                               sleep=sleeps.append)
    run = out["run"]
    budget = project.config.search.max_retries
    assert len([r for r in requests if r["cursorMark"] == "SYNCURSOR001"]) == budget + 1
    assert len(sleeps) == budget
    assert (run["status"], run["retrieved_count"], run["provider_total"]) == ("incomplete", 4, 10)
    assert run["resume_cursor"] == "SYNCURSOR001"
    assert run["errors"][-1]["kind"] == "retrieval_failed" and "HTTP 503" in run["errors"][-1]["message"]
    assert "can be resumed" in run["completeness_note"]
    assert "incomplete" in run["label"] and "retrieved 4 of 10" in run["label"]

    resumed = europepmc.run_search(project, OPERATOR, mode="fixture", resume_run_id=run["id"],
                                   transport=cursor_transport(SS.europepmc_pages(4)), sleep=lambda s: None)["run"]
    assert resumed["id"] == run["id"]
    assert (resumed["status"], resumed["retrieved_count"], resumed["resume_cursor"]) == ("complete", 10, None)
    assert resumed["errors"][0]["kind"] == "retrieval_failed"         # the earlier problems stay on the record
    positions = [r[0] for r in project.conn.execute(
        "SELECT position FROM search_result WHERE search_run_id = ? ORDER BY position", (run["id"],))]
    assert positions == list(range(10))
    actions = [e["action"] for e in audit.history(project.conn, entity_type="search_run", entity_id=run["id"])]
    assert actions.count("search.resume") == 1 and actions.count("search.finish") == 2
    with pytest.raises(GeodesicError, match="nothing to resume"):
        europepmc.run_search(project, OPERATOR, mode="fixture", resume_run_id=run["id"],
                             transport=cursor_transport(SS.europepmc_pages(4)))


def test_non_retryable_status_is_not_retried(project):
    requests: list[dict] = []
    transport = cursor_transport(SS.europepmc_pages(4), requests=requests, failures={"*": [httpx.Response(400)]})
    run = _run(project, transport)["run"]
    assert len(requests) == 1
    assert (run["status"], run["retrieved_count"], run["provider_total"]) == ("incomplete", 0, None)
    assert "unknown" in run["label"]                                   # provider total never learnt


def test_retrieval_limit_truncates_and_can_be_resumed_with_a_higher_limit(project):
    out = _run(project, cursor_transport(SS.europepmc_pages(4)), max_records=6)
    run = out["run"]
    assert (run["status"], run["retrieved_count"], run["provider_total"], run["max_records"]) == \
        ("truncated", 6, 10, 6)
    assert "retrieval limit of 6" in run["completeness_note"]
    assert "4 record(s) reported by the provider were not retrieved" in run["completeness_note"]
    assert snapshot.verify_snapshot(project, run["id"])["intact"]

    resumed = europepmc.run_search(project, OPERATOR, mode="fixture", resume_run_id=run["id"], max_records=50,
                                   transport=cursor_transport(SS.europepmc_pages(4)), sleep=lambda s: None)
    assert resumed["run"]["status"] == "complete" and resumed["run"]["retrieved_count"] == 10
    assert resumed["links"]["new_paper"] == 4                          # nothing imported twice
    hashes = [r[0] for r in project.conn.execute(
        "SELECT record_sha256 FROM search_result WHERE search_run_id = ?", (run["id"],))]
    assert len(hashes) == len(set(hashes)) == 10
    snap = snapshot.read_snapshot(project, run["id"])
    assert snap["intact"] and [r["parsed"]["title"] for r in snap["records"]] == [r["title"] for r in SS.records()]


def test_limit_on_a_page_boundary_is_truncated_only_when_more_is_reported(project):
    run = _run(project, cursor_transport(SS.europepmc_pages(4)), max_records=8)["run"]
    assert (run["status"], run["retrieved_count"], run["resume_cursor"]) == ("truncated", 8, "SYNCURSOR002")
    run = _run(project, cursor_transport(SS.europepmc_pages(5)), max_records=10, page_size=5)["run"]
    assert (run["status"], run["retrieved_count"]) == ("complete", 10)


def test_fewer_records_than_the_provider_reported_is_incomplete(project):
    pages = SS.europepmc_pages(4, hit_count=25)
    run = _run(project, cursor_transport(pages, end_with_empty_page=True))["run"]
    assert (run["status"], run["retrieved_count"], run["provider_total"]) == ("incomplete", 10, 25)
    assert "provider reported 25 records but pagination ended after 10" in run["completeness_note"]


def test_unrecognised_response_is_stored_but_not_imported(project):
    pages = SS.europepmc_pages(4)
    pages[1] = json.dumps({"geodesicFixture": TAG_TEXT, "unexpected": True}).encode()
    run = _run(project, cursor_transport(pages))["run"]
    assert (run["status"], run["retrieved_count"], run["raw_responses"]) == ("incomplete", 4, 2)
    assert run["errors"][-1]["kind"] == "unrecognised_response"
    assert "stored but not imported" in run["completeness_note"]
    assert snapshot.verify_snapshot(project, run["id"])["intact"]
    with pytest.raises(europepmc.ResponseSchemaError):
        europepmc.parse_page(b"<html>not json</html>")
    with pytest.raises(europepmc.ResponseSchemaError):
        europepmc.parse_page(b'{"hitCount": "many", "resultList": {"result": []}}')


def test_snapshot_reread_detects_changed_and_missing_raw_responses(project):
    run = _run(project, cursor_transport(SS.europepmc_pages(4)))["run"]
    first = snapshot.read_snapshot(project, run["id"])
    again = snapshot.read_snapshot(project, run["id"])
    assert first["intact"] and first == again                          # re-reading is exactly repeatable
    assert first["recorded_results"] == len(first["records"]) == 10
    stored = [project.abs(r["stored_path"]) for r in project.conn.execute(
        "SELECT stored_path FROM search_raw_response WHERE search_run_id = ? ORDER BY seq", (run["id"],))]
    assert all(not (p.stat().st_mode & 0o222) for p in stored)         # stored read-only
    stored[1].chmod(0o644)
    stored[1].write_bytes(stored[1].read_bytes().replace(b"SYNCURSOR002", b"SYNCURSOR00X"))
    report = snapshot.verify_snapshot(project, run["id"])
    assert not report["intact"] and "no longer matches its recorded SHA-256" in report["problems"][0]
    stored[1].unlink()
    report = snapshot.verify_snapshot(project, run["id"])
    assert not report["intact"] and "missing" in report["problems"][0]


def test_identical_records_in_a_second_run_link_to_the_same_papers(project):
    first = _run(project, cursor_transport(SS.europepmc_pages(4)))
    second = _run(project, cursor_transport(SS.europepmc_pages(4, variant=2)), query=QUERY + " AND tank")
    assert first["run"]["id"] != second["run"]["id"]
    assert second["links"] == {"new_paper": 1, "exact_duplicate_record": 1, "validated_identifier_match": 1}
    # the paper found by both runs keeps both memberships
    shared = project.conn.execute(
        "SELECT paper_id FROM search_result GROUP BY paper_id HAVING COUNT(DISTINCT search_run_id) = 2").fetchall()
    assert len(shared) == 2
    assert project.conn.execute("SELECT COUNT(*) FROM paper WHERE origin = 'search'").fetchone()[0] == 11


def test_live_retrieval_is_refused_in_a_synthetic_project(project):
    with pytest.raises(ProvenanceError):
        europepmc.run_search(project, OPERATOR, query=QUERY, mode="live")
    assert project.conn.execute("SELECT COUNT(*) FROM search_run").fetchone()[0] == 0


def test_untagged_fixture_responses_need_a_declaration(project, tmp_path):
    untagged = []
    for page in SS.europepmc_pages(4):
        body = json.loads(page)
        body.pop("geodesicFixture")
        untagged.append(json.dumps(body).encode())
    with pytest.raises(ProvenanceError):
        _run(project, cursor_transport(untagged))
    failed = runs.list_runs(project.conn)[0]
    assert failed["status"] == "failed" and failed["retrieved_count"] == 0
    assert project.conn.execute("SELECT COUNT(*) FROM paper").fetchone()[0] == 0
    out = _run(project, cursor_transport(untagged), declare="synthetic")
    assert out["run"]["status"] == "complete" and out["run"]["declared_data_class"] == "synthetic"
    # a directory of stored pages works the same way
    directory = tmp_path / "pages"
    directory.mkdir()
    for i, page in enumerate(untagged):
        (directory / f"p{i}.json").write_bytes(page)
    with pytest.raises(ProvenanceError):
        europepmc.run_search(project, OPERATOR, query=QUERY, fixture_dir=directory)


def test_synthetic_fixture_responses_are_rejected_by_a_real_project(real_project, tmp_path):
    directory = SS.write_europepmc_fixture(tmp_path / "fx", page_size=4)
    with pytest.raises(ProvenanceError):
        europepmc.run_search(real_project, OPERATOR, query=QUERY, fixture_dir=directory)
    with pytest.raises(ProvenanceError):
        europepmc.run_search(real_project, OPERATOR, query=QUERY, fixture_dir=directory, declare="real")
    assert real_project.conn.execute("SELECT COUNT(*) FROM paper").fetchone()[0] == 0


def test_query_translation_is_visible_and_configurable(project):
    concepts = [ConceptGroup(label="tracer", terms=["inert tracer", "dye"]),
                ConceptGroup(label="system", terms=["reservoir"])]
    q = europepmc.build_query(project, None, concepts, {"year_from": 2015, "year_to": 2020, "open_access": True})
    assert q["translated_query"] == '("inert tracer" OR dye) AND reservoir'
    assert q["executed_query"] == '(("inert tracer" OR dye) AND reservoir) AND PUB_YEAR:[2015 TO 2020] AND' \
                                  ' OPEN_ACCESS:y'
    assert q["concept_query"] == [g.model_dump() for g in concepts]
    assert q["filters"] == {"year_from": 2015, "year_to": 2020, "open_access": True,
                            "derivation": "translation of configured concept groups"}
    assert europepmc.translate(concepts, "TITLE") == '(TITLE:"inert tracer" OR TITLE:dye) AND TITLE:reservoir'
    # an explicit source-specific query wins and the concept translation is still shown next to it
    q = europepmc.build_query(project, "TITLE:tracer", concepts)
    assert q["executed_query"] == "TITLE:tracer" and q["translated_query"] == '("inert tracer" OR dye) AND reservoir'
    project.config.search.provider_query_override["europepmc"] = "ABSTRACT:tracer"
    assert europepmc.build_query(project, None, concepts)["executed_query"] == "ABSTRACT:tracer"
    project.config.search.provider_query_override.clear()
    with pytest.raises(ConfigError, match="No search query"):
        europepmc.build_query(project, None, [])


def test_provider_record_fields_are_kept_as_supplied(project):
    _run(project, cursor_transport(SS.europepmc_pages(4)))
    raw = SS.records()[0]
    rec = europepmc.parse_record(raw)
    assert rec.title == raw["title"] and rec.year == int(raw["pubYear"])
    assert rec.provider_record_id == f"{raw['source']}:{raw['id']}"
    paper = project.conn.execute("SELECT * FROM paper WHERE title = ?", (raw["title"],)).fetchone()
    assert paper["origin"] == "search" and paper["identity_status"] == "identifier_validated"
    ident = project.conn.execute("SELECT validation_status, validated_via FROM paper_identifier WHERE paper_id = ?"
                                 " AND scheme = 'doi'", (paper["id"],)).fetchone()
    assert tuple(ident) == ("validated", "provider_record:europepmc")
