"""Command line for search, search-file import, Crossref validation, duplicate handling and screening.

Every command goes through ``geodesic.cli.main`` exactly as the installed
``geodesic`` command does. All inputs are generated synthetic fixtures.
"""

from __future__ import annotations

import sqlite3

import pytest

from geodesic import protocol as P
from geodesic.cli import main
from geodesic.config import dump_yaml
from geodesic.synthetic import search as SS
from geodesic.synthetic.protocol import example_protocol
from geodesic.synthetic.workbook import example_config, fixture_mapping, write_fixture_workbook
from geodesic.workbook.mapping import write_mapping

TA = "title_abstract"
QUERY = '("inert tracer" OR "dye tracer" OR tracer) AND (reservoir OR tank OR conduit)'


class Workspace:
    """A synthetic project directory plus generated input files, driven only through the command line."""

    def __init__(self, root, capsys):
        self.root, self.capsys = root, capsys
        self.proj = str(root / "proj")
        self.cfg = root / "project.yaml"
        dump_yaml(example_config().model_dump(mode="json"), self.cfg)
        self.workbook = write_fixture_workbook(root / "wb" / "w.xlsx", version=1)
        self.mapping = root / "mapping.yaml"
        write_mapping(fixture_mapping(confirmed=True), self.mapping)
        self.fixture_dir = SS.write_europepmc_fixture(root / "fx1", page_size=4, variant=1)
        self.ris = SS.write_ris_fixture(root / "files" / "export.ris")
        self.csv = SS.write_csv_fixture(root / "files" / "export.csv")
        self.protocol = root / "protocol.yaml"
        self.protocol.write_text(P.dump_protocol(example_protocol(True)), encoding="utf-8")

    def run(self, *args, reviewer="Synthetic Reviewer A", expect=0):
        """Run one command; return (stdout, stderr). The exit code is asserted."""
        self.capsys.readouterr()
        code = main([*map(str, args), "--project", self.proj, "--reviewer", reviewer])
        out = self.capsys.readouterr()
        assert code == expect, f"exit {code}, expected {expect}\n{out.out}\n{out.err}"
        return out.out, out.err

    def one(self, sql, *params):
        conn = sqlite3.connect(self.proj + "/geodesic.sqlite")
        try:
            row = conn.execute(sql, params).fetchone()
        finally:
            conn.close()
        return row

    def scalar(self, sql, *params):
        return self.one(sql, *params)[0]


@pytest.fixture
def ws(tmp_path, capsys):
    w = Workspace(tmp_path, capsys)
    assert main(["init", "--project", w.proj, "--config", str(w.cfg), "--data-class", "synthetic"]) == 0
    w.run("import-workbook", w.workbook, "--mapping", w.mapping)
    out, _ = w.run("protocol", "load", w.protocol)
    assert "v2 (PROVISIONAL)" in out
    return w


@pytest.fixture
def searched_ws(ws):
    ws.run("search", "run", "--fixture-dir", ws.fixture_dir, "--max-records", "50")
    return ws


# ------------------------------------------------------------------ search
def test_search_commands_record_and_resume_a_run(ws):
    out, _ = ws.run("search", "translate")
    assert f"executed query: {QUERY}" in out and "translated for Europe PMC" in out

    out, _ = ws.run("search", "run", "--fixture-dir", ws.fixture_dir, "--max-records", "6")
    rid = ws.scalar("SELECT id FROM search_run")
    assert "[SYNTHETIC project" in out and "protocol v2 (PROVISIONAL)" in out
    assert f"search run {rid}: europepmc · FIXTURE (synthetic responses) · truncated · retrieved 6 of 10 reported" in out
    assert "exploratory (protocol provisional)" in out
    assert f"executed query: {QUERY}" in out
    assert "pages retrieved: 2   raw responses stored: 2" in out
    assert "stopped at the retrieval limit of 6 records" in out
    assert "4 record(s) reported by the provider were not retrieved" in out
    assert f"resume with: geodesic search resume {rid}" in out
    assert "No search is exhaustive." in out

    out, _ = ws.run("search", "list")
    assert rid in out and "truncated" in out and QUERY in out

    out, _ = ws.run("search", "resume", rid, "--fixture-dir", ws.fixture_dir, "--max-records", "50")
    assert "complete · retrieved 10 of 10 reported" in out and "pages retrieved: 4" in out
    assert "new_paper: 4" in out
    assert ws.scalar("SELECT COUNT(*) FROM search_run") == 1          # resumed, not a second run
    assert ws.scalar("SELECT COUNT(*) FROM search_result") == 10

    out, _ = ws.run("search", "show", rid, "--limit", "3")
    assert "interface: Europe PMC RESTful Web Service GET /search" in out
    assert "page size: 100   retrieval limit: 50   pages: 4" in out
    assert "contact address supplied: no" in out
    assert "first 3 of 10 records:" in out and "Tracer release from a stirred source tank (2019)" in out

    out, _ = ws.run("search", "verify")
    assert f"{rid}  intact  4 raw response(s), 10 record(s) re-read of 10 recorded" in out


def test_live_search_is_refused_in_a_synthetic_project(ws):
    out, err = ws.run("search", "run", "--query", "x", expect=2)
    assert "not permitted in a synthetic project" in err
    assert ws.scalar("SELECT COUNT(*) FROM search_run") == 0 and ws.scalar("SELECT COUNT(*) FROM search_result") == 0


# ------------------------------------------------------------------ search-file import
def test_search_file_import_commands(searched_ws):
    ws = searched_ws
    out, _ = ws.run("import-search-file", ws.ris, "--source-label", "synthetic export")
    assert "file:ris · FILE IMPORT · complete · retrieved 4 of unknown reported" in out
    assert "new_paper: 4" in out
    assert "record 4 has no ER tag" in out                      # the parser's note is shown, nothing is dropped
    assert ws.scalar("SELECT COUNT(*) FROM search_run WHERE provider = 'file:ris'") == 1

    out, _ = ws.run("import-search-file", ws.csv)
    assert "file:csv · FILE IMPORT · complete · retrieved 3 of unknown reported" in out
    assert "columns kept with the stored record but not interpreted: Extra column" in out

    # the same bytes again: recognised by hash, nothing changes
    before = ws.scalar("SELECT COUNT(*) FROM paper"), ws.scalar("SELECT COUNT(*) FROM search_result")
    out, _ = ws.run("import-search-file", ws.ris)
    assert "This exact file was already imported; nothing was changed." in out
    assert (ws.scalar("SELECT COUNT(*) FROM paper"), ws.scalar("SELECT COUNT(*) FROM search_result")) == before
    assert ws.scalar("SELECT COUNT(*) FROM search_run WHERE provider = 'file:ris'") == 1


def test_untagged_search_file_needs_a_declaration(searched_ws, tmp_path):
    ws = searched_ws
    untagged = tmp_path / "untagged.csv"
    untagged.write_text("Title,Year\nUntagged synthetic entry,2021\n", encoding="utf-8")
    runs = ws.scalar("SELECT COUNT(*) FROM search_run")
    _, err = ws.run("import-search-file", untagged, expect=2)
    assert "declare" in err.lower()
    assert ws.scalar("SELECT COUNT(*) FROM search_run") == runs


# ------------------------------------------------------------------ Crossref validation
def test_crossref_validation_commands(searched_ws):
    ws = searched_ws
    out, _ = ws.run("crossref", "validate", "--synthetic-fixture")
    assert "[SYNTHETIC fixture responses]" in out
    assert "validated 3, not found in Crossref 2, possible misassigned identifier 1, errors 0" in out
    assert "It does not verify what a workbook says about the paper." in out
    # the workbook record and the provider record of the same validated DOI are now grouped
    assert ws.scalar("SELECT COUNT(*) FROM paper_merge WHERE status = 'active' AND"
                     " basis = 'validated_identifier_match'") == 1

    _, err = ws.run("crossref", "validate", expect=2)
    assert "not permitted in a synthetic project" in err


# ------------------------------------------------------------------ duplicate handling
def test_dedup_commands(searched_ws):
    ws = searched_ws
    out, _ = ws.run("dedup", "run")
    assert "identifier_matches:" in out and "candidates_similarity:" in out

    out, _ = ws.run("dedup", "list", "--limit", "3")
    assert "relation(s) with status 'candidate'" in out
    assert "title_author_year_similarity" in out and "'weights_used'" in out and "'threshold': 0.8" in out

    rel, a, b = ws.one("SELECT id, paper_a, paper_b FROM paper_relation WHERE status = 'candidate' AND"
                       " relation_type = 'duplicate' AND method = 'title_author_year_similarity'"
                       " ORDER BY score DESC, id")
    out, _ = ws.run("dedup", "decide", rel, "confirmed", "--note", "same record")
    assert f"relation {rel}: confirmed; records grouped (mrg_" in out
    merge, member, canonical = ws.one("SELECT id, member_paper_id, canonical_paper_id FROM paper_merge"
                                      " WHERE relation_id = ?", rel)
    assert {member, canonical} == {a, b}
    assert ws.scalar("SELECT canonical_id FROM paper WHERE id = ?", member) == canonical

    out, _ = ws.run("dedup", "groups")
    assert f"{merge}  active  {member} under {canonical}  basis reviewer_confirmed_duplicate  by Synthetic Reviewer A" \
        in out

    out, _ = ws.run("dedup", "ungroup", member, "--reason", "trial reversal")
    assert f"grouping {merge} reversed" in out
    status, reason, by = ws.one("SELECT status, reversal_reason, reversed_by FROM paper_merge WHERE id = ?", merge)
    assert (status, reason, by) == ("reversed", "trial reversal", "Synthetic Reviewer A")
    assert ws.scalar("SELECT canonical_id FROM paper WHERE id = ?", member) is None   # stands on its own again
    assert ws.scalar("SELECT COUNT(*) FROM paper WHERE id IN (?, ?)", a, b) == 2     # both records still exist

    # a file record carrying the same (unvalidated) DOI as a provider record is a candidate, confirmed in bulk
    ws.run("import-search-file", ws.ris)
    out, _ = ws.run("dedup", "confirm-identifier-matches")
    assert out.startswith("confirmed ") and "left for individual review" in out
    assert int(out.split()[1].rstrip(";")) >= 1

    out, _ = ws.run("audit", "verify")
    assert "hash chain intact" in out


# ------------------------------------------------------------------ screening
def test_screening_commands_under_a_provisional_then_confirmed_protocol(searched_ws):
    ws = searched_ws
    out, _ = ws.run("screen", "suggest", "--stage", TA)
    assert "generator protocol-rules/1, protocol v2 provisional" in out
    assert "Suggestions are drafts. None of them is a decision." in out
    assert ws.scalar("SELECT COUNT(*) FROM screening_suggestion") > 0
    assert ws.scalar("SELECT COUNT(*) FROM screening_decision") == 0

    out, _ = ws.run("screen", "queue", "--stage", TA, "--limit", "3")
    assert "awaiting a title_abstract decision" in out and "suggestion sug_" in out

    # while the protocol is provisional: no automated exclusions and no final decisions
    _, err = ws.run("screen", "automate", "--stage", TA, expect=2)
    assert "Automated exclusions are not enabled while the protocol is provisional" in err
    sug, excluded = ws.one("SELECT id, paper_id FROM screening_suggestion WHERE status = 'open' AND"
                           " suggested_decision = 'exclude' AND criterion_id = 'E2'")
    _, err = ws.run("screen", "accept", sug, "--final", expect=2)
    assert "cannot be finalised while the protocol is provisional" in err
    assert ws.scalar("SELECT COUNT(*) FROM screening_decision") == 0

    out, _ = ws.run("screen", "accept", sug)
    assert f"suggestion accepted as your decision: exclude (PROVISIONAL) for {excluded}" in out
    assert ws.one("SELECT decision, finality, reviewer, decider_kind, suggestion_id FROM screening_decision") == \
        ("exclude", "provisional", "Synthetic Reviewer A", "human", sug)

    # two named reviewers, one after the other, disagree; the disagreement is kept and then adjudicated
    pid = ws.scalar("SELECT paper_id FROM screening_suggestion WHERE status = 'open' AND"
                    " suggested_decision = 'include' ORDER BY id")
    out, _ = ws.run("screen", "decide", pid, "--stage", TA, "--decision", "include")
    assert f"recorded include (PROVISIONAL) for {pid} at title_abstract; current outcome: include" in out
    out, _ = ws.run("screen", "decide", pid, "--stage", TA, "--decision", "uncertain", "--reason-kind",
                    "insufficient_information", "--reason", "method not stated", reviewer="Synthetic Reviewer B")
    assert "current outcome: conflict" in out
    out, _ = ws.run("screen", "conflicts")
    assert "1 paper(s) with a recorded disagreement" in out
    assert "by Synthetic Reviewer A [human]" in out and "by Synthetic Reviewer B [human]" in out
    assert "reason: method not stated" in out
    out, _ = ws.run("screen", "adjudicate", pid, "--stage", TA, "--decision", "include", "--reason",
                    "agreed after discussion")
    assert "current outcome: include" in out
    assert ws.scalar("SELECT COUNT(*) FROM screening_decision WHERE paper_id = ?", pid) == 3     # nothing erased

    # an exclusion that rests only on missing information is refused at this stage
    _, err = ws.run("screen", "decide", pid, "--stage", TA, "--decision", "exclude", "--reason-kind",
                    "insufficient_information", "--reason", "abstract does not say", expect=2)
    assert err.startswith("error:")

    out, _ = ws.run("protocol", "confirm")
    assert "Protocol confirmed: v2 (confirmed)" in out

    out, _ = ws.run("screen", "automate", "--stage", TA)
    assert "automated decisions recorded: 2 {'date_limit': 1, 'publication_type': 1}" in out
    assert "rules authorised by protocol v2: publication_type, date_limit" in out
    assert ws.scalar("SELECT COUNT(*) FROM screening_decision WHERE decider_kind = 'automated'") == 2

    out, _ = ws.run("screen", "rereview")
    assert "4 decision(s) flagged for re-review" in out
    assert "NEEDS RE-REVIEW: recorded under a provisional protocol; protocol v2 is now confirmed" in out
    first = ws.scalar("SELECT id FROM screening_decision WHERE is_current = 1 AND needs_rereview = 1 AND"
                      " paper_id = ?", excluded)
    out, _ = ws.run("screen", "rereview", "--reaffirm", first, "--note", "still applies")
    assert "decision kept and recorded again under protocol v2 (final)" in out

    out, _ = ws.run("screen", "history", excluded)
    assert "2 decision(s), 1 suggestion(s)" in out
    assert "provisional, protocol v2  (superseded)" in out and "final, protocol v2" in out
    assert "re-reviewed and kept: still applies" in out
    assert 'support: "Reactive tracer" title chars 0-15' in out

    out, _ = ws.run("screen", "summary")
    assert "Protocol: v2 (confirmed)" in out
    assert "current decisions — human: 4, automated: 2, simulated: 0" in out
    assert "papers decided only by automation: 2" in out
    assert "superseded decisions kept in history: 1" in out
    assert "excluded — criterion_not_met [E2] by human decision: 1 paper(s)" in out
    assert "excluded — criterion_not_met [date_limit] by automated decision: 1 paper(s)" in out
    assert "no search is exhaustive" in out

    out, _ = ws.run("audit", "verify")
    assert "hash chain intact" in out


def test_assess_command_keeps_dimensions_separate_from_eligibility(searched_ws):
    ws = searched_ws
    pid = ws.scalar("SELECT id FROM paper WHERE origin = 'search' ORDER BY pk")
    out, _ = ws.run("assess", pid, "--dimension", "primary_relevance", "--value", "direct", "--rationale", "trial")
    assert "recorded primary_relevance = direct (pas_" in out
    out, _ = ws.run("assess", pid)
    assert "primary_relevance: direct  by Synthetic Reviewer A  — trial" in out
    assert ws.scalar("SELECT COUNT(*) FROM screening_decision") == 0       # an assessment is not an eligibility decision
    _, err = ws.run("assess", pid, "--dimension", "primary_relevance", "--value", "excellent", expect=2)
    assert err.startswith("error:")


def test_reviewer_identity_is_required_for_decisions(searched_ws, monkeypatch):
    ws = searched_ws
    pid = ws.scalar("SELECT id FROM paper WHERE origin = 'search' ORDER BY pk")
    ws.capsys.readouterr()
    code = main(["screen", "decide", pid, "--stage", TA, "--decision", "include", "--project", ws.proj])
    err = ws.capsys.readouterr().err
    assert code == 2 and "reviewer" in err.lower()
    assert ws.scalar("SELECT COUNT(*) FROM screening_decision") == 0
