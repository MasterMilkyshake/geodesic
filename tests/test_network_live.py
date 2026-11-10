"""Bounded LIVE checks of the provider integrations (deselected by default).

Run with::

    python -m pytest tests -m network

These tests contact Europe PMC (search and open-access full-text XML) and Crossref. They check retrieval mechanics,
counts and response schema only; nothing about the content of the returned
records is printed, stored in the source tree or asserted. Each test works in a
throw-away project of data class ``real`` in the system temporary directory,
which is deleted afterwards. A failure here means "live operation could not be
verified", not that the stored-snapshot logic is wrong (that is covered offline).
"""

from __future__ import annotations

import pytest

from geodesic.search import smoke

pytestmark = pytest.mark.network


@pytest.fixture(scope="module")
def report():
    return smoke.run_in_temporary_project()


def _by_name(report):
    return {c["check"]: c for c in report["checks"]}


def test_live_search_pagination_limit_and_snapshot(report):
    checks = _by_name(report)
    for name in ("search request answered in the documented shape (hitCount present)",
                 "retrieved count equals min(retrieval limit, provider total)",
                 "cursor pagination followed across pages",
                 "run status reflects the retrieval limit",
                 "stored raw responses re-read with matching SHA-256 and identical records",
                 "every record carries the identifying fields 'id' and 'source'"):
        assert checks[name]["ok"] is True, f"{name}: {checks[name]['detail']}"
    assert report["live"] is True and report["project_kept_at"] is None
    assert report["search_run"]["retrieved_count"] <= report["max_records"]


def test_live_filter_syntax(report):
    checks = _by_name(report)
    for name in ("PUB_YEAR range filter is honoured", "OPEN_ACCESS filter is honoured",
                 "filtered total does not exceed the unfiltered total"):
        assert checks[name]["ok"] is True, f"{name}: {checks[name]['detail']}"


def test_live_crossref_lookup(report):
    name = "Crossref work lookup answers in the documented shape with the requested DOI"
    check = _by_name(report)[name]
    if check["ok"] is None:
        pytest.skip(f"could not be evaluated: {check['detail']}")
    assert check["ok"] is True, check["detail"]


def test_live_open_access_fulltext_retrieval(report):
    name = "open-access full-text XML retrieved by PMCID, stored with its hash and parsed into addressable units"
    check = _by_name(report)[name]
    if check["ok"] is None:
        pytest.skip(f"could not be evaluated: {check['detail']}")
    assert check["ok"] is True, check["detail"]
    info = report["fulltext"]
    assert info["text_units"] > 0 and info["stored_copy_matches_response_hash"] is True
    assert set(info) <= {"provider", "attempts", "outcomes", "bytes", "parse_status", "text_units", "tables",
                         "license_supplied", "stored_copy_matches_response_hash"}       # no document content


def test_report_contains_mechanics_only(report):
    """The report holds counts, flags and the configured query; no record content."""
    text = smoke.format_report(report)
    assert "record content is not analysed" in text
    assert set(report["record_field_presence"]) == {"records", "fields"}
    assert all(isinstance(v, int) for v in report["record_field_presence"]["fields"].values())
