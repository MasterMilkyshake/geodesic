"""Bounded live smoke test of the provider integrations.

The test checks **retrieval mechanics, counts and response schema only**. It
never prints, summarises or analyses the content of the records it retrieves:
the report contains counts, status codes, field-presence tallies and hashes.

It needs a project of data class ``real`` because live responses describe real
publications. :func:`run_in_temporary_project` creates a throw-away real project
in the system temporary directory (outside any source tree) and deletes it
afterwards, so nothing retrieved is kept.

The checks cover: cursor pagination and the retrieval limit, the stored raw
snapshot, the year and open-access filters, one Crossref work lookup and one
open-access full-text XML retrieval (Europe PMC ``fullTextXML``; sizes, parse
status and unit counts only).

Query, record limit and page size come from configuration
(``search.smoke_query``, ``search.smoke_max_records``, ``search.smoke_page_size``).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

from ..config import ProjectConfig
from ..errors import GeodesicError
from ..project import Project, create_project
from ..util import Actor, utc_now
from . import crossref, europepmc, snapshot

SMOKE_ACTOR = Actor("geodesic-live-smoke", "system")
_RECORD_FIELDS = ("id", "source", "title", "authorString", "pubYear", "doi", "pmid", "pmcid", "isOpenAccess",
                  "abstractText", "pubTypeList", "language", "license")


def _check(checks: list[dict], name: str, ok: bool | None, detail: str) -> None:
    checks.append({"check": name, "ok": ok, "detail": detail})


def _raw_records(project: Project, run_id: str) -> list[dict]:
    return [json.loads(r["raw_json"]) for r in project.conn.execute(
        "SELECT raw_json FROM search_result WHERE search_run_id = ? ORDER BY position", (run_id,))]


def live_smoke(project: Project, actor: Actor = SMOKE_ACTOR, *, with_crossref: bool = True,
               with_fulltext: bool = True) -> dict[str, Any]:
    """Run the bounded live checks in ``project`` (data class ``real``). Returns a report of mechanics only.

    ``ok`` is ``None`` for a check that could not be evaluated (reported as
    unverified, never as passed).
    """
    cfg = project.config.search
    checks: list[dict] = []
    report: dict[str, Any] = {"started_at": utc_now(), "provider": europepmc.PROVIDER, "query": cfg.smoke_query,
                              "max_records": cfg.smoke_max_records, "page_size": cfg.smoke_page_size,
                              "checks": checks, "live": True}
    # 1. cursor pagination, retrieval limit, stored snapshot
    out = europepmc.run_search(project, actor, query=cfg.smoke_query, max_records=cfg.smoke_max_records,
                               page_size=cfg.smoke_page_size, mode="live")
    run = out["run"]
    report["search_run"] = {k: run[k] for k in ("status", "retrieved_count", "provider_total", "pages_retrieved",
                                                "raw_responses", "executed_query", "exploratory")}
    report["search_run"]["errors"] = [e.get("kind") for e in run["errors"]]
    expected = None if run["provider_total"] is None else min(cfg.smoke_max_records, run["provider_total"])
    _check(checks, "search request answered in the documented shape (hitCount present)",
           run["provider_total"] is not None, f"provider-reported total: {run['provider_total']}")
    _check(checks, "retrieved count equals min(retrieval limit, provider total)",
           expected is not None and run["retrieved_count"] == expected,
           f"retrieved {run['retrieved_count']}, expected {expected}")
    want_pages = None if expected is None else -(-expected // cfg.smoke_page_size)
    _check(checks, "cursor pagination followed across pages",
           (run["pages_retrieved"] == want_pages and want_pages >= 2) if want_pages is not None else None,
           f"{run['pages_retrieved']} page(s) retrieved, {want_pages} expected"
           + ("" if (want_pages or 0) >= 2 else " (fewer than 2 pages: pagination not exercised)"))
    _check(checks, "run status reflects the retrieval limit",
           run["status"] == ("truncated" if (run["provider_total"] or 0) > cfg.smoke_max_records else "complete"),
           f"status '{run['status']}'")
    snap = snapshot.verify_snapshot(project, run["id"])
    _check(checks, "stored raw responses re-read with matching SHA-256 and identical records", snap["intact"],
           f"{snap['raw_responses']} raw response(s), {snap['records']} record(s) re-read; problems:"
           f" {len(snap['problems'])}")
    recs = _raw_records(project, run["id"])
    presence = {f: sum(1 for r in recs if r.get(f) not in (None, "")) for f in _RECORD_FIELDS}
    report["record_field_presence"] = {"records": len(recs), "fields": presence}
    _check(checks, "every record carries the identifying fields 'id' and 'source'",
           bool(recs) and presence["id"] == len(recs) and presence["source"] == len(recs),
           f"id on {presence['id']}/{len(recs)}, source on {presence['source']}/{len(recs)}")
    # 2. filter syntax (PUB_YEAR range, OPEN_ACCESS flag)
    year = 2020
    f = europepmc.run_search(project, actor, query=cfg.smoke_query, filters={"year_from": year, "year_to": year,
                                                                           "open_access": True},
                             max_records=cfg.smoke_page_size, page_size=cfg.smoke_page_size, mode="live")
    frun = f["run"]
    frecs = _raw_records(project, frun["id"])
    report["filtered_run"] = {k: frun[k] for k in ("status", "retrieved_count", "provider_total", "executed_query")}
    years_ok = bool(frecs) and all(str(r.get("pubYear")) == str(year) for r in frecs)
    oa_ok = bool(frecs) and all(r.get("isOpenAccess") == "Y" for r in frecs)
    narrower = None if None in (frun["provider_total"], run["provider_total"]) else \
        frun["provider_total"] <= run["provider_total"]
    _check(checks, "PUB_YEAR range filter is honoured", years_ok if frecs else None,
           f"{sum(str(r.get('pubYear')) == str(year) for r in frecs)}/{len(frecs)} returned records have pubYear"
           f" {year}")
    _check(checks, "OPEN_ACCESS filter is honoured", oa_ok if frecs else None,
           f"{sum(r.get('isOpenAccess') == 'Y' for r in frecs)}/{len(frecs)} returned records have isOpenAccess 'Y'")
    _check(checks, "filtered total does not exceed the unfiltered total", narrower,
           f"filtered {frun['provider_total']}, unfiltered {run['provider_total']}")
    # 3. Crossref work lookup for one DOI the search returned (identifier validation mechanics)
    if with_crossref:
        report["crossref"] = _crossref_check(project, checks, [r["doi"] for r in recs + frecs if r.get("doi")][:3])
    # 4. open-access full-text XML for one returned open-access record (retrieval and parsing mechanics)
    if with_fulltext:
        report["fulltext"] = _fulltext_check(project, actor, checks, frun["id"])
    report["finished_at"] = utc_now()
    report["passed"] = sum(1 for c in checks if c["ok"] is True)
    report["failed"] = sum(1 for c in checks if c["ok"] is False)
    report["unverified"] = sum(1 for c in checks if c["ok"] is None)
    return report


def _crossref_check(project: Project, checks: list[dict], dois: list[str]) -> dict[str, Any]:
    info: dict[str, Any] = {"provider": crossref.PROVIDER, "lookups": 0, "answered": 0, "not_found": 0}
    name = "Crossref work lookup answers in the documented shape with the requested DOI"
    if not dois:
        _check(checks, name, None, "no returned record carried a DOI, so no lookup was made")
        return info
    client = crossref.make_client(project)
    try:
        for doi in dois:
            info["lookups"] += 1
            try:
                resp = client.get(f"{crossref.BASE_URL}/works/{urllib.parse.quote(doi.lower(), safe='/')}")
            except GeodesicError as exc:
                if "HTTP 404" in str(exc):
                    info["not_found"] += 1      # a DOI of another registration agency: not an error
                    continue
                _check(checks, name, False, f"lookup failed: {type(exc).__name__}")
                return info
            work = crossref.parse_work(json.loads(resp.content.decode("utf-8"))["message"])
            info["answered"] += 1
            _check(checks, name, work["doi"] == doi.lower(),
                   f"HTTP {resp.status}; returned DOI {'matches' if work['doi'] == doi.lower() else 'differs'};"
                   f" title present: {work['title'] is not None}; updated-by notices listed: {len(work['notices'])}")
            return info
    finally:
        client.close()
    _check(checks, name, None, f"none of {info['lookups']} DOI(s) is registered with Crossref (HTTP 404)")
    return info


def _fulltext_check(project: Project, actor: Actor, checks: list[dict], run_id: str, attempts: int = 3) -> dict[str, Any]:
    """Retrieve full-text XML for at most ``attempts`` open-access records of a run until one is attached.

    Reports status, sizes and unit counts only; the retrieved text is not printed or analysed, and it
    is deleted with the throw-away project.
    """
    from ..documents import openaccess

    info: dict[str, Any] = {"provider": openaccess.PROVIDER, "attempts": 0, "outcomes": []}
    name = "open-access full-text XML retrieved by PMCID, stored with its hash and parsed into addressable units"
    papers = [r["paper_id"] for r in project.conn.execute(
        "SELECT DISTINCT sr.paper_id FROM search_result sr JOIN paper p ON p.id = sr.paper_id WHERE"
        " sr.search_run_id = ? AND p.pmcid IS NOT NULL ORDER BY sr.position", (run_id,))][:attempts]
    if not papers:
        _check(checks, name, None, "no returned open-access record carried a PMCID, so no retrieval was made")
        return info
    for paper_id in papers:
        info["attempts"] += 1
        try:
            out = openaccess.fetch_open_access(project, paper_id, actor, live=True)
        except GeodesicError as exc:
            info["outcomes"].append(f"refused: {type(exc).__name__}")
            continue
        info["outcomes"].append(out["outcome"])
        if out["outcome"] != "attached":
            continue
        snap = out["snapshot"]
        stored = project.conn.execute("SELECT sha256, stored_path FROM document WHERE id = ?",
                                      (out["document_id"],)).fetchone()
        from ..util import sha256_file

        intact = sha256_file(project.abs(stored["stored_path"])) == stored["sha256"] == out["sha256"]
        info.update(bytes=out["bytes"], parse_status=snap["status"], text_units=snap["n_units"],
                    tables=snap["n_tables"], license_supplied=bool(out["license_in_document"]),
                    stored_copy_matches_response_hash=intact)
        _check(checks, name, snap["status"] in ("ok", "partial") and snap["n_units"] > 0 and intact,
               f"attempt {info['attempts']}: {out['bytes']} bytes; parse status '{snap['status']}';"
               f" {snap['n_units']} text unit(s), {snap['n_tables']} table(s); licence stated in the document:"
               f" {'yes' if out['license_in_document'] else 'no'}; stored copy matches the response hash: {intact}")
        return info
    _check(checks, name, None if all(o == "retrieval_failed" for o in info["outcomes"]) else False,
           f"{info['attempts']} attempt(s), none attached: {', '.join(info['outcomes'])}")
    return info


def run_in_temporary_project(config: ProjectConfig | None = None, *, keep_dir: Path | None = None,
                             with_crossref: bool = True, with_fulltext: bool = True) -> dict[str, Any]:
    """Run :func:`live_smoke` in a throw-away real project and delete it afterwards.

    With ``keep_dir`` the project is created there and kept (it then holds real
    bibliographic records and must stay outside source control).
    """
    from ..config import StageConfig

    config = config or ProjectConfig(name="live smoke test", stages=[StageConfig(key="s1", name="Stage 1")])
    root = Path(keep_dir).expanduser() if keep_dir else Path(tempfile.mkdtemp(prefix="geodesic-live-smoke-")) / "p"
    project = create_project(root, config, "real", SMOKE_ACTOR)
    try:
        report = live_smoke(project, with_crossref=with_crossref, with_fulltext=with_fulltext)
        report["project_kept_at"] = str(root) if keep_dir else None
        return report
    finally:
        project.close()
        if not keep_dir:
            shutil.rmtree(root.parent, ignore_errors=True)


def format_report(report: dict[str, Any]) -> str:
    lines = [f"LIVE smoke test ({report['provider']}), started {report['started_at']}",
             f"query (from configuration): {report['query']}   limit: {report['max_records']} records,"
             f" page size {report['page_size']}",
             "Only retrieval mechanics, counts and response schema are checked; record content is not analysed."]
    for c in report["checks"]:
        mark = {True: "PASS", False: "FAIL", None: "UNVERIFIED"}[c["ok"]]
        lines.append(f"  [{mark}] {c['check']} — {c['detail']}")
    lines.append(f"{report['passed']} passed, {report['failed']} failed, {report['unverified']} unverified")
    return "\n".join(lines)
