"""Synthetic inputs for the scale smoke test, and the timed run itself.

Everything generated here is invented (an inert tracer moving among engineered
reservoirs; DOI prefix ``10.5555`` is reserved for examples) and carries the
synthetic tag. The generator is deterministic for a given seed.

``run_scale`` drives the ordinary application functions (fixture search import,
duplicate detection, workbook import through a mapping, queries, evidence maps,
validation, table export, audit-chain verification) on a synthetic project of
the requested size and returns the measured wall-clock time of each step. It
measures; it does not assert performance targets.
"""

from __future__ import annotations

import datetime as dt
import json
import platform
import random
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable

from openpyxl import Workbook
from openpyxl.packaging.custom import StringProperty

from ..provenance import SYNTHETIC_LABEL, TAG_PROPERTY, TAG_TEXT
from ..util import Actor
from .workbook import HEADER_ROW, HEADERS, STAGES, example_config, fixture_mapping

SCALE_ACTOR = Actor("scale-smoke-test", "system")

_PROCESS = ("Release", "Washout", "Dispersion", "Retention", "Breakthrough", "Recirculation", "Dilution", "Settling",
            "Clearance", "Mixing", "Transit", "Stratification", "Bypass", "Drawdown", "Accumulation", "Carryover")
_TRACER = ("an inert dye tracer", "a salt tracer", "a fluorescent tracer", "a conservative tracer", "a pulse tracer",
           "a stepped tracer", "a dual tracer", "a thermal tracer", "a particulate tracer", "a dissolved gas tracer")
_STRUCTURE = ("stirred source tank", "baffled conduit", "holding reservoir", "outflow weir", "bypass line",
              "mixing loop", "settling basin", "return conduit", "lined tank", "storage pond", "header tank",
              "overflow channel", "sump", "manifold", "siphon", "standpipe", "spillway", "gallery")
_CONDITION = ("low flow", "high flow", "pulsed inflow", "steady inflow", "partial filling", "full filling",
              "cold operation", "warm operation", "intermittent pumping", "continuous pumping", "throttled outlet",
              "open outlet", "staged drawdown", "rapid drawdown")
_QUALIFIER = ("pilot", "bench", "full-scale", "replicated", "paired", "sequential", "seasonal", "overnight",
              "long-run", "short-run", "instrumented", "retrofitted")
_SURNAME = ("Arden", "Belmont", "Castellan", "Dunmore", "Eastlake", "Fenwick", "Garrow", "Hallam", "Ingleby",
            "Jarrow", "Kestrel", "Lorimer", "Marlow", "Norcross", "Oakes", "Quillon", "Rowntree", "Sable",
            "Thackeray", "Ullswater", "Varley", "Wexcombe", "Yarrow", "Zennor", "Ashdown", "Brackley", "Coverdale",
            "Dunster", "Elstow", "Farleigh", "Glaisdale", "Harcourt", "Ilderton", "Kelby", "Langthorne", "Mossley")
_JOURNAL = ("Journal of Synthetic Hydraulics", "Synthetic Reservoir Engineering",
            "Proceedings of the Synthetic Flow Conference", "Synthetic Tracer Letters")
_OUTCOME = ("Tracer release", "Retained fraction", "Transit signal", "Discharge clearance", "Residual concentration",
            "Dispersion", "Peak arrival", "Mixing completeness")
_UNITS = ("mg/L", "mL/min", "L/min", "1/h", "mg/kg", "%")
_LIMITS = ("Single site; not independently verified.", "Liner batch not reported.", "Detection limit not stated.",
           "Flow meter calibration not described.", "Sampling interval not reported.")


def scale_doi(i: int) -> str:
    return f"10.5555/geodesic.scale.{i:06d}"


def _title(rng: random.Random, i: int) -> str:
    return (f"{rng.choice(_PROCESS)} of {rng.choice(_TRACER)} in a {rng.choice(_QUALIFIER)} {rng.choice(_STRUCTURE)} "
            f"under {rng.choice(_CONDITION)}: {rng.choice(_QUALIFIER)} series {i:05d}")


def _authors(rng: random.Random) -> list[str]:
    return [f"{rng.choice(_SURNAME)} {chr(65 + rng.randrange(26))}" for _ in range(rng.randint(1, 4))]


def scale_papers(n: int, seed: int = 20240101) -> list[dict[str, Any]]:
    """``n`` invented bibliographic records (title, authors, year, journal, doi)."""
    rng = random.Random(seed)
    return [{"index": i, "title": _title(rng, i), "authors": _authors(rng), "year": 1990 + rng.randrange(35),
             "journal": rng.choice(_JOURNAL), "doi": scale_doi(i)} for i in range(n)]


def write_scale_search_fixture(directory: Path | str, papers: list[dict[str, Any]], page_size: int = 100,
                               duplicate_every: int = 250) -> dict[str, int]:
    """Write provider-shaped response pages for ``papers`` (plus some repeated records) and return counts.

    Every ``duplicate_every``-th paper is returned a second time as a record
    from another source: alternately with the same DOI (an identifier match)
    and with no identifier and a slightly different title (a similarity
    candidate for a reviewer).
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    recs: list[dict[str, Any]] = []
    same_doi = near_title = 0
    for p in papers:
        i = p["index"]
        recs.append({
            "id": f"SCL{i:07d}", "source": "MED", "pmid": str(80_000_000 + i), "doi": p["doi"], "title": p["title"],
            "authorString": ", ".join(p["authors"]) + ".",
            "authorList": {"author": [{"fullName": a} for a in p["authors"]]},
            "journalInfo": {"journal": {"title": p["journal"]}}, "pubYear": str(p["year"]),
            "pubTypeList": {"pubType": ["research-article"]}, "isOpenAccess": "N", "language": "eng",
            "citedByCount": 0,
            "abstractText": f"Synthetic abstract {i:05d}: {p['title'].lower()} was followed in repeated runs."})
        if duplicate_every and i % duplicate_every == duplicate_every - 1:
            dup = dict(recs[-1], id=f"SCD{i:07d}", source="AGR")
            dup.pop("pmid")
            if (i // duplicate_every) % 2 == 0:
                same_doi += 1
            else:
                dup.pop("doi")
                dup["title"] = p["title"].replace(" of ", " of the ", 1) + "."
                near_title += 1
            recs.append(dup)
    chunks = [recs[k:k + page_size] for k in range(0, len(recs), page_size)] or [[]]
    for k, chunk in enumerate(chunks):
        body = {"geodesicFixture": TAG_TEXT, "version": "synthetic-fixture", "hitCount": len(recs),
                "nextCursorMark": f"SCLCURSOR{k + 1:05d}", "nextPageUrl": None,
                "request": {"queryString": "synthetic", "resultType": "core",
                            "cursorMark": "*" if k == 0 else f"SCLCURSOR{k:05d}", "pageSize": page_size},
                "resultList": {"result": chunk}}
        (directory / f"page_{k + 1:05d}.json").write_text(json.dumps(body), encoding="utf-8")
    return {"records": len(recs), "pages": len(chunks), "repeated_same_doi": same_doi,
            "repeated_near_title_no_identifier": near_title}


def write_scale_workbook(path: Path | str, papers: list[dict[str, Any]], n_rows: int, seed: int = 20240102
                         ) -> dict[str, int]:
    """Write a fixture-layout workbook with ``n_rows`` evidence rows spread over the stage sheets.

    Each row holds one narrative sentence with a value, one cell with a
    measured value and, on every second row, a limitation: 2.5 statements per
    row on average. Returns the counts the generator put in.
    """
    rng = random.Random(seed)
    wb = Workbook(write_only=True)
    readme = wb.create_sheet("README")
    readme.append([SYNTHETIC_LABEL])
    readme.append(["Invented scale-test fixture. No paper, author, journal, identifier or value in this workbook "
                   "exists."])
    per_sheet = [n_rows // len(STAGES) + (1 if s < n_rows % len(STAGES) else 0) for s in range(len(STAGES))]
    counts = {"rows": 0, "narrative_statements": 0, "value_cells": 0, "limitations": 0}
    sheet_rows: list[tuple[str, int, int]] = []
    g = 0
    for s, (_, name, _) in enumerate(STAGES):
        ws = wb.create_sheet(name)
        ws.append([f"{name} evidence (synthetic scale fixture)"])
        ws.append([])
        ws.append(list(HEADERS))
        assert HEADER_ROW == 3
        seen: set[int] = set()
        for _ in range(per_sheet[s]):
            p = papers[(g * 7919 + s) % len(papers)]
            seen.add(p["index"])
            citation = (f"{', '.join(p['authors'])}. {p['title']}. {p['journal']}. {p['year']}.")
            unit = rng.choice(_UNITS)
            value = round(rng.uniform(0.1, 400.0), rng.choice((0, 1, 2)))
            value_text = f"{value:g}"
            hours = rng.choice((1, 2, 6, 12, 24, 48, 72))
            runs = rng.randint(3, 9)
            narrative = (f"{rng.choice(_OUTCOME)} was {value_text} {unit} at {hours} h in run set {g:06d} "
                         f"(n = {runs} runs).")
            sd = round(value * rng.uniform(0.02, 0.2), 2) or 0.01
            measured = f"{round(value * rng.uniform(0.5, 1.5), 1):g} ± {sd:g} {unit} (SD)"
            limitation = rng.choice(_LIMITS) if g % 2 == 0 else None
            ws.append([citation, p["doi"], f"{rng.choice(_QUALIFIER).capitalize()} {rng.choice(_STRUCTURE)}",
                       narrative, measured, limitation, None, p["year"]])
            counts["rows"] += 1
            counts["narrative_statements"] += 1
            counts["value_cells"] += 1
            counts["limitations"] += 1 if limitation else 0
            g += 1
        sheet_rows.append((name, per_sheet[s], len(seen)))
    summary = wb.create_sheet("Summary")
    summary.append(["Stage", "Rows", "Papers"])
    for name, rows, n_papers in sheet_rows:
        summary.append([name, rows, n_papers])
    summary.append(["Total", n_rows, len({(k * 7919 + s) % len(papers)
                                          for s, k in _row_indices(per_sheet)})])
    wb.custom_doc_props.append(StringProperty(name=TAG_PROPERTY, value="synthetic"))
    fixed = dt.datetime(2000, 1, 1)
    wb.properties.created = fixed
    wb.properties.modified = fixed
    wb.properties.creator = "GEODESIC synthetic fixture generator"
    wb.properties.title = SYNTHETIC_LABEL
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    counts["papers_referenced"] = len({(k * 7919 + s) % len(papers) for s, k in _row_indices(per_sheet)})
    counts["statements"] = counts["narrative_statements"] + counts["value_cells"] + counts["limitations"]
    return counts


def _row_indices(per_sheet: list[int]):
    g = 0
    for s, n in enumerate(per_sheet):
        for _ in range(n):
            yield s, g
            g += 1


def environment() -> dict[str, Any]:
    """Where a measurement was taken (no user or host names)."""
    import os

    return {"python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version, "platform": platform.platform(),
            "machine": platform.machine(), "processor": platform.processor() or None, "cpu_count": os.cpu_count()}


def run_scale(directory: Path | str, n_papers: int = 10_000, n_findings: int = 100_000, *,
              workbook_paper_share: float = 0.6, n_validated: int = 2000, n_screened: int = 500,
              n_verified: int = 200,
              echo: Callable[[str], None] = print) -> dict[str, Any]:
    """Build a synthetic project of the requested size with the ordinary functions and time each step.

    The workbook cites ``workbook_paper_share`` of the papers. The DOIs of
    ``n_validated`` of them are then validated against synthetic
    registry-shaped responses (no network). The search returns all papers, so
    its import links records to validated workbook papers, raises duplicate
    candidates for the unvalidated ones and creates new records for the rest.
    ``n_screened`` papers are
    screened and ``n_verified`` findings verified by a *simulated* reviewer
    (synthetic projects only) to time the per-action cost.
    """
    from .. import audit, dedup, protocol, query, review, screening
    from .. import findings as F
    from ..export import export_tables
    from ..extraction import validation
    from ..project import create_project
    from ..reports import figures, flow, maps, missing
    from ..search import crossref, europepmc
    from ..workbook.importer import import_workbook
    from .protocol import example_protocol
    from .search import _work, crossref_transport

    directory = Path(directory)
    if (directory / "project").exists():
        raise FileExistsError(f"{directory / 'project'} already exists; give an empty directory.")
    directory.mkdir(parents=True, exist_ok=True)
    timings: list[dict[str, Any]] = []
    reviewer = Actor("Simulated Scale Reviewer", "simulated")
    note = "simulated review action of the scale smoke test"

    def timed(label: str, fn: Callable[[], Any]) -> Any:
        start = time.perf_counter()
        out = fn()
        seconds = round(time.perf_counter() - start, 3)
        timings.append({"step": label, "seconds": seconds})
        echo(f"  {seconds:9.2f} s  {label}")
        return out

    echo(f"{SYNTHETIC_LABEL}\nScale smoke test: {n_papers} papers, about {n_findings} findings")
    n_rows = -(-n_findings * 2 // 5)                       # 2.5 statements per row
    papers = timed("generate synthetic paper records", lambda: scale_papers(n_papers))
    cited = papers[:max(1, int(n_papers * workbook_paper_share))]
    fixture = timed("write synthetic search response pages",
                    lambda: write_scale_search_fixture(directory / "inputs" / "search", papers))
    wb_path = directory / "inputs" / "scale_workbook.xlsx"
    wb_counts = timed("write synthetic workbook", lambda: write_scale_workbook(wb_path, cited, n_rows))

    cfg = example_config("Synthetic scale test")
    project = timed("create project", lambda: create_project(directory / "project", cfg, "synthetic", SCALE_ACTOR))
    try:
        conn = project.conn
        result = timed(f"import workbook ({wb_counts['rows']} evidence rows) through the mapping",
                       lambda: import_workbook(project, wb_path, fixture_mapping(True), actor=SCALE_ACTOR))
        works = {p["doi"]: _work(p["doi"], p["title"], [tuple(a.rsplit(" ", 1)) for a in p["authors"]],
                                 p["year"], p["journal"]) for p in cited}
        n_val = min(n_validated, len(cited))
        valid = timed(f"validate {n_val} workbook DOIs against synthetic registry-shaped responses",
                      lambda: crossref.validate_identifiers(project, SCALE_ACTOR, limit=n_val,
                                                            transport=crossref_transport(works),
                                                            sleep=lambda s: None))
        search = timed(
            f"import {fixture['records']} search records ({fixture['pages']} fixture pages) incl. duplicate detection",
            lambda: europepmc.run_search(project, SCALE_ACTOR, query="synthetic", page_size=100, mode="fixture",
                                         max_records=fixture["records"] + 1,
                                         fixture_dir=directory / "inputs" / "search", sleep=lambda s: None))
        run_id = search["run"]["id"]
        dd = timed("duplicate detection over all paper records (repeat run)",
                   lambda: dedup.find_duplicates(conn, project.config.dedup))
        grouped = timed("confirm identifier matches with near-identical titles (simulated reviewer)",
                        lambda: dedup.confirm_identifier_candidates(conn, reviewer, note=note))
        rel = timed("duplicate candidates awaiting review, first page",
                    lambda: dedup.relations(conn, status="candidate"))

        def confirm_protocol():
            protocol.save_new_version(conn, example_protocol(authorize_automation=True), reviewer, note)
            return protocol.confirm(conn, reviewer, project.data_class)
        timed("save and confirm the synthetic example protocol (simulated reviewer)", confirm_protocol)
        sug = timed("title/abstract screening suggestions for all papers",
                    lambda: screening.suggest(conn, "title_abstract"))
        queue = timed("screening queue, first page", lambda: screening.queue(conn, "title_abstract", limit=50))
        timed("screening queue restricted to the search run, first page",
              lambda: screening.queue(conn, "title_abstract", search_run_id=run_id, limit=50))
        todo = [r["id"] for r in screening.queue(conn, "title_abstract", limit=n_screened)["rows"]]

        def screen():
            for pid in todo:
                screening.decide(conn, pid, "title_abstract", "include", reviewer, reason_kind="meets_criteria",
                                 reason_text=note)
            return len(todo)
        n_dec = timed(f"{len(todo)} simulated title/abstract decisions", screen)
        summ = timed("search and screening flow summary", lambda: screening.summary(conn))
        timed("search and screening flow table and figure",
              lambda: (flow.flow_table(summ), figures.flow_figure(summ).svg()))

        counts = timed("project counts (overview)", lambda: query.counts(conn))
        page = timed("full-text search of findings ('clearance'), first page",
                     lambda: query.search_findings(conn, query.FindingFilter(text="clearance"), limit=50))
        staged = timed("filter findings by stage and quantitative, first page",
                       lambda: query.search_findings(
                           conn, query.FindingFilter(stage_keys=[STAGES[1][0]], quantitative=True), limit=50))
        timed("all findings ordered by paper, page 1,000 of 50",
              lambda: query.search_findings(conn, limit=50, offset=min(49_950, max(0, counts["findings"] - 50))))
        ppage = timed("full-text search of papers ('weir'), first page",
                      lambda: query.search_papers(conn, query.PaperFilter(text="weir"), limit=50))
        timed("papers of the search run with findings, first page",
              lambda: query.search_papers(conn, query.PaperFilter(search_run_id=run_id, has_findings=True),
                                          limit=50))
        one = page.rows[0]["id"] if page.rows else None
        if one:
            timed("open one finding with its source locations", lambda: F.get_finding(conn, one))
        checks = {name: "confirmed" for name in review.REQUIRED_CHECKS}
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM finding WHERE record_status = 'active' AND grounding_status = 'grounded' AND"
            " kind = 'measurement' ORDER BY pk LIMIT ?", (n_verified,))]

        def verify():
            for fid in ids:
                review.verify(conn, fid, reviewer, checks, note=note, acknowledge_flags=True)
            return len(ids)
        n_ver = timed(f"{len(ids)} simulated verifications of findings against stored source text", verify)
        emap = timed("evidence map (stage by system/context)", lambda: maps.evidence_map(conn, "system_context"))
        omap = timed("evidence map (stage by outcome)", lambda: maps.evidence_map(conn, "outcome"))
        timed("evidence map figure (SVG)", lambda: figures.evidence_map_figure(emap).svg())
        miss = timed("missing-information summary", lambda: missing.missing_information(conn))
        timed("modelling inventory", lambda: missing.modeling_inventory(conn))
        val = timed("validation run", lambda: validation.run_validation(project, SCALE_ACTOR))
        exp = timed("export all tables (CSV, JSON)",
                    lambda: export_tables(project, directory / "export", ("csv", "json"), actor=SCALE_ACTOR))
        chain = timed("verify audit hash chain", lambda: audit.verify_chain(conn))
        db_bytes = project.db_path.stat().st_size
        report = {
            "label": SYNTHETIC_LABEL, "requested": {"papers": n_papers, "findings": n_findings},
            "environment": environment(), "generated": {"search": fixture, "workbook": wb_counts},
            "observed": {
                "paper_records": counts["reports"],
                "paper_records_grouped_under_a_canonical_record": counts["reports_grouped_under_canonical"],
                "findings": counts["findings"],
                "search_records_retrieved": search["run"].get("retrieved_count"),
                "identifier_validation": {k: valid.get(k) for k in (
                    "looked_up", "validated", "not_found", "possible_misassignment")},
                "search_links": search.get("links"), "duplicate_detection_after_search": search.get("dedup"),
                "duplicate_detection_repeat": dd, "identifier_matches_confirmed": grouped,
                "duplicate_candidates_awaiting_review": rel.get("total"),
                "workbook_reconciliation": {k: result.stats.get(k) for k in (
                    "cells_read", "evidence_rows", "papers", "findings", "unparsed_content", "parse_failures")},
                "screening_suggestions": {k: sug.get(k) for k in ("papers_considered", "created", "by_suggestion")},
                "screening_queue_total": queue.get("total"), "simulated_screening_decisions": n_dec,
                "simulated_verifications": n_ver,
                "fts_matches_clearance": page.total, "stage_filter_matches": staged.total,
                "paper_matches_weir": ppage.total,
                "evidence_map_columns": {"system_context": len(emap.get("columns", [])),
                                         "outcome": len(omap.get("columns", []))},
                "missing_information_rows": len(miss.get("rows", [])),
                "validation": {k: val.get(k) for k in ("n_findings", "n_issues", "by_severity")},
                "export_rows": exp["rows"], "audit_chain_ok": chain[0], "audit_events": chain[1],
                "database_bytes": db_bytes,
            },
            "timings": timings, "total_seconds": round(sum(t["seconds"] for t in timings), 3),
        }
    finally:
        project.close()
    (directory / "scale_report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    echo(f"  total {report['total_seconds']:.1f} s; database {db_bytes / 1e6:.0f} MB; report: "
         f"{directory / 'scale_report.json'}")
    return report
