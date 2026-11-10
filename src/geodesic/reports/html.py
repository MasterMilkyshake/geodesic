"""Self-contained HTML report.

One file, no scripts, no external resources: styles are inline and figures are
embedded as data URIs, so the report can be opened offline and archived as is.
Every value that comes from a workbook, a document or a provider is written
through :func:`html.escape`; document content is data and can never become
markup. A Content-Security-Policy forbids scripts and network access as a
second line of defence.

The numbers are the ones the CLI prints: the report calls
:func:`geodesic.screening.summary`, :func:`geodesic.reports.maps.evidence_map`,
:func:`geodesic.reports.missing.missing_information` and the other public
functions, and embeds the figures of :mod:`geodesic.reports.figures`.

The page is light-only by design (``color-scheme: light``): the embedded
figures are drawn for a light surface.
"""

from __future__ import annotations

import base64
from dataclasses import asdict
from html import escape
from typing import Any, Iterable

from .. import __version__, audit, comparison, papers, query, screening
from ..export import input_hashes
from .. import locations
from ..export import tables as T
from ..export import writers as W
from ..extraction import validation
from ..project import Project
from ..project import stages as project_stages
from ..provenance import SYNTHETIC_LABEL
from ..util import loads, utc_now
from . import figures as F
from . import flow as FL
from . import maps as M
from . import missing as MI

LIMITATIONS = (
    "This report organizes records; it draws no conclusion from them. Nothing is pooled, averaged, ranked or "
    "modelled.",
    "Counts describe the records in this project. No search is exhaustive, and an empty cell of an evidence map "
    "means only that nothing is recorded there.",
    "Findings imported from a workbook are imported summaries. Only review status 'verified' records that a named "
    "reviewer confirmed a finding against its source, and only for the stated version of the finding.",
    "Grounding and validation check that a finding can be traced to a stored source location and is internally "
    "consistent. They do not establish that the finding represents its source correctly.",
    "The application cannot detect undisclosed synthetic content. The data class shown is the class declared when "
    "the project was created and when each file was imported.",
    "Correction and retraction information is shown as 'none found in <source> on <date>' or 'not checked'; it is "
    "never a statement that a report has not been corrected or retracted.",
    "Independence of findings is assessed only where a reviewer assigned an experiment or related findings as "
    "dependent; 'independent units, at most' is an upper bound.",
)

CSS = """
:root { color-scheme: light; --surface: #fcfcfb; --ink: #0b0b0b; --ink2: #52514e; --muted: #898781;
        --grid: #e1e0d9; --axis: #c3c2b7; --neutral: #f0efec; --warn: #ffe699; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--surface); color: var(--ink);
       font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
main { max-width: 1120px; margin: 0 auto; padding: 0 24px 64px; }
.banner { background: var(--warn); color: var(--ink); font-weight: 700; padding: 10px 24px; border-bottom: 1px solid var(--axis); }
.banner.plain { background: var(--neutral); font-weight: 600; }
h1 { font-size: 26px; margin: 28px 0 4px; }
h2 { font-size: 20px; margin: 40px 0 8px; padding-top: 16px; border-top: 1px solid var(--grid); }
h3 { font-size: 16px; margin: 24px 0 6px; }
p { margin: 6px 0; max-width: 78ch; }
.meta, .note { color: var(--ink2); }
.note { font-size: 13.5px; }
nav ul { list-style: none; padding: 0; margin: 12px 0 0; display: flex; flex-wrap: wrap; gap: 4px 16px; }
nav a { color: #1c5cab; }
.tiles { display: flex; flex-wrap: wrap; gap: 12px; margin: 16px 0; }
.tile { border: 1px solid var(--grid); border-radius: 8px; padding: 10px 14px; min-width: 150px; background: #fff; }
.tile .n { font-size: 26px; font-weight: 700; line-height: 1.2; }
.tile .l { color: var(--ink2); font-size: 13px; }
.scroll { overflow-x: auto; margin: 10px 0; }
table { border-collapse: collapse; font-size: 13.5px; }
caption { text-align: left; color: var(--ink2); padding: 0 0 6px; caption-side: top; }
th, td { border-bottom: 1px solid var(--grid); padding: 5px 9px; text-align: left; vertical-align: top; }
thead th { border-bottom: 1px solid var(--axis); color: var(--ink2); font-weight: 600; white-space: nowrap; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td { white-space: nowrap; }
td.wide { min-width: 220px; max-width: 380px; white-space: normal; }
td.id { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; color: var(--ink2);
        white-space: nowrap; }
table.heat { border-collapse: separate; border-spacing: 2px; }
table.heat th, table.heat td { border: 0; }
table.heat td.cell { text-align: center; min-width: 64px; border-radius: 3px; font-variant-numeric: tabular-nums; }
table.heat td.empty { background: var(--neutral); color: var(--muted); }
table.heat td.total, table.heat th.total { color: var(--ink2); text-align: right; }
table.heat tbody th { font-weight: 400; max-width: 340px; }
figure { margin: 12px 0; }
figure img { max-width: 100%; height: auto; border: 1px solid var(--grid); border-radius: 6px; }
details { margin: 8px 0; }
summary { cursor: pointer; color: #1c5cab; }
.status { display: inline-block; padding: 0 6px; border: 1px solid var(--axis); border-radius: 10px; font-size: 12px;
          white-space: nowrap; }
.status.verified { border-color: var(--ink); font-weight: 600; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12.5px; }
ul.plain { padding-left: 20px; max-width: 82ch; }
@media print { nav { display: none; } h2 { break-after: avoid; } .scroll { overflow: visible; } }
"""


def _e(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return escape(f"{value:.12g}")
    return escape(str(value))


def _table(columns: list[tuple[str, str]], rows: Iterable[dict[str, Any]], *, caption: str | None = None,
           numeric: Iterable[str] = (), wide: Iterable[str] = (), ids: Iterable[str] = (),
           empty: str = "No records.") -> str:
    """``columns`` are ``(key, heading)`` pairs. Everything is escaped."""
    numeric, wide, ids = set(numeric), set(wide), set(ids)
    rows = list(rows)
    if not rows:
        return f'<p class="note">{_e(empty)}</p>'
    out = ['<div class="scroll"><table>']
    if caption:
        out.append(f"<caption>{_e(caption)}</caption>")
    out.append("<thead><tr>" + "".join(
        f'<th scope="col"{" class=num" if k in numeric else ""}>{_e(h)}</th>' for k, h in columns) + "</tr></thead>")
    out.append("<tbody>")
    for r in rows:
        cells = []
        for k, _ in columns:
            cls = "num" if k in numeric else "wide" if k in wide else "id" if k in ids else ""
            cells.append(f"<td{f' class={cls}' if cls else ''}>{_e(r.get(k))}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def _notes(notes: Iterable[str]) -> str:
    return "".join(f'<p class="note">{_e(n)}</p>' for n in notes)


def _figure(canvas: F.Canvas, alt: str) -> str:
    data = base64.b64encode(canvas.render("svg")).decode("ascii")
    return (f'<figure><img alt="{_e(alt)}" width="{int(canvas.width)}" height="{int(canvas.height)}" '
            f'src="data:image/svg+xml;base64,{data}"></figure>')


def figure_banner(info: dict[str, Any]) -> str | None:
    """Label drawn inside every figure of a synthetic project."""
    return SYNTHETIC_LABEL if info["data_class"] == "synthetic" else None


# ------------------------------------------------------------------ sections
def _heatmap(emap: dict[str, Any], metric: str = "findings") -> str:
    """Evidence map as an HTML table (terms as rows, stages as columns); the table is its own table view."""
    stages, terms = emap["rows"], emap["columns"]
    if not terms or not stages:
        return '<p class="note">No findings are recorded.</p>'
    vmax = max([M.cell(emap, s["key"], t["key"], metric) for s in stages for t in terms] + [1])
    out = ['<div class="scroll"><table class="heat">',
           f"<caption>{_e(emap['title'])} — number of {_e(M.METRIC_LABELS[metric])}. Darker means more; "
           "“·” means nothing is recorded in this project.</caption>",
           f'<thead><tr><th scope="col">{_e(emap["field_title"])}</th>'
           + "".join(f'<th scope="col">{_e(s["label"])}</th>' for s in stages)
           + '<th scope="col" class="total">all stages</th></tr></thead><tbody>']
    for t in terms:
        label = _e(t["label"])
        if t["kind"] == "mapped":
            label += f' <span class="note">(reviewer mapping of: {_e("; ".join(t["original_terms"]))})</span>'
        elif t["kind"] == "other":
            label += f' <span class="note">({_e("; ".join(t["original_terms"]))})</span>'
        row = [f'<tr><th scope="row">{label}</th>']
        for s in stages:
            v = M.cell(emap, s["key"], t["key"], metric)
            if v:
                fill = F.sequential_colour(v, vmax)
                tip = f"{s['label']} × {t['label']}: {v} {M.METRIC_LABELS[metric]}"
                row.append(f'<td class="cell" style="background:{fill};color:{F.ink_on(fill)}" title="{_e(tip)}">'
                           f"{v}</td>")
            else:
                row.append('<td class="cell empty">·</td>')
        row.append(f'<td class="total">{t[metric]}</td></tr>')
        out.append("".join(row))
    out.append('<tr><th scope="row" class="total">all terms</th>'
               + "".join(f'<td class="total" style="text-align:center">{s[metric]}</td>' for s in stages)
               + f'<td class="total"><strong>{emap["total"][metric]}</strong></td></tr>')
    out.append("</tbody></table></div>")
    return "".join(out)


def _stated(f: dict[str, Any]) -> str:
    text = f.get("value_literal") or ""
    unit = f.get("unit_original") or ""
    return f"{text} {unit}".strip()


def _finding_rows(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for f in findings:
        primary = next((l for l in f.get("locations", []) if l["role"] == "supporting_excerpt"), None)
        rows.append({
            "citation": papers.citation_label({
                "authors": loads(f.get("paper_authors_json"), []), "year": f.get("paper_year"),
                "citation_text": f.get("paper_citation"), "title": f.get("paper_title"), "id": f["paper_id"]}),
            "stages": "; ".join(f.get("stages", [])), "kind": f["kind"],
            "source_wording": locations.short_excerpt(f.get("source_wording")),
            "outcome": f.get("outcome"), "system_context": f.get("system_context"), "as_stated": _stated(f),
            "timepoint": f.get("timepoint_literal"), "review_status": f["review_status"],
            "grounding_status": f["grounding_status"],
            "source_location": T.location_text(primary) if primary else "no source location",
            "finding_id": f["id"]})
    return rows


def render_report(project: Project, *, flt: query.FindingFilter | None = None, title: str | None = None,
                  max_findings: int = 500, max_papers: int = 500) -> str:
    """The report as one HTML string. ``flt`` narrows the findings shown and counted in maps and summaries."""
    conn = project.conn
    info = T.project_info(project)
    counts = info["counts"]
    fbanner = figure_banner(info)
    summary = screening.summary(conn)
    emaps = {by: M.evidence_map(conn, by, flt=flt) for by in ("system_context", "outcome")}
    miss = MI.missing_information(conn, flt)
    inv = MI.modeling_inventory(conn)
    ft = MI.fulltext_summary(conn)
    val = validation.summary(conn)
    sets = comparison.list_sets(conn)
    page = query.search_findings(conn, flt, limit=max_findings, with_locations=True)
    ok, n_events, problem = audit.verify_chain(conn)
    hashes = input_hashes(project)
    title = title or f"Evidence report: {project.name}"
    filtered = flt is not None and flt != query.FindingFilter()

    h: list[str] = []
    add = h.append
    add("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">")
    add('<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; '
        'img-src data:">')
    add('<meta name="viewport" content="width=device-width, initial-scale=1">')
    add(f'<meta name="generator" content="GEODESIC {_e(__version__)}">')
    add(f"<title>{_e(('SYNTHETIC — ' if project.is_synthetic else '') + title)}</title><style>{CSS}</style></head><body>")
    warn = project.is_synthetic or not info["protocol_confirmed"]
    add(f'<div class="banner{"" if warn else " plain"}" role="note">{_e(W.banner(info))}</div><main>')
    add(f"<h1>{_e(title)}</h1>")
    add(f'<p class="meta">Generated {_e(utc_now())} by GEODESIC {_e(__version__)} · project ID '
        f"<code>{_e(project.id)}</code> · data class <strong>{_e(project.data_class)}</strong> · protocol "
        f"<strong>{_e(info['protocol_version'])}</strong></p>")
    if project.is_synthetic:
        add("<p><strong>Everything in this report is synthetic.</strong> It was generated from fixtures to show "
            "how the application works and is not research evidence. Review actions by simulated reviewers are "
            "labelled as simulated.</p>")
    if not info["protocol_confirmed"]:
        add("<p><strong>The protocol is provisional.</strong> Eligibility criteria have not been confirmed; "
            "screening decisions are drafts, and searches made under this protocol are exploratory.</p>")
    if filtered:
        active = {k: v for k, v in asdict(flt).items() if v is not None and not (k == "record_statuses" and v == ["active"])}
        add(f"<p><strong>Filtered report.</strong> Findings, evidence maps and the missing-information summary are "
            f"restricted to: <code>{_e(active)}</code>. Search, screening and document sections describe the "
            "whole project.</p>")
    sections = [("counts", "Counts"), ("flow", "Search and screening"), ("fulltext", "Full text"),
                ("maps", "Evidence maps"), ("missing", "Missing information"), ("modeling", "Modelling readiness"),
                ("comparison", "Comparison sets"), ("validation", "Validation"), ("findings", "Evidence table"),
                ("papers", "Reports"), ("inputs", "Inputs and audit trail"), ("limits", "Limitations")]
    add('<nav aria-label="Sections"><ul>' + "".join(f'<li><a href="#{k}">{_e(v)}</a></li>' for k, v in sections)
        + "</ul></nav>")

    # counts
    add('<h2 id="counts">Counts</h2>')
    add("<p>Reports, studies, experiments and findings are different things and are counted separately.</p>")
    review = counts["findings_by_review_status"]
    tiles = [("Reports (papers)", counts["reports"]), ("Findings", counts["findings"]),
             ("Findings verified by a reviewer", review.get("verified", 0)),
             ("Studies", counts["studies"]), ("Experiments", counts["experiments"]),
             ("Documents", counts["documents"]), ("Search runs", counts["search_runs"])]
    add('<div class="tiles">' + "".join(
        f'<div class="tile"><div class="n">{_e(n)}</div><div class="l">{_e(label)}</div></div>'
        for label, n in tiles) + "</div>")
    add(_table([("item", "Item"), ("n", "Count")], [
        {"item": "Reports whose citation is unresolved", "n": counts["reports_unresolved"]},
        {"item": "Records grouped under another record as duplicates", "n": counts["reports_grouped_under_canonical"]},
        {"item": "Findings not assigned to an experiment", "n": counts["findings_without_experiment"]},
        {"item": "Findings without a stage", "n": counts["findings_without_stage"]},
        *({"item": f"Findings with review status '{k}'", "n": v} for k, v in sorted(review.items())),
        *({"item": f"Findings of kind '{k}'", "n": v} for k, v in sorted(counts["findings_by_kind"].items())),
    ], numeric=["n"]))
    stages = project_stages(conn)
    if stages:
        add("<h3>Stages</h3>")
        add(_table([("key", "Key"), ("name", "Stage"), ("definition", "Definition")],
                   [{"key": s["key"], "name": s["name"], "definition": s.get("definition")} for s in stages],
                   caption="Stages are defined in the project configuration.", wide=["definition"]))

    # flow
    add('<h2 id="flow">Search and screening</h2>')
    add(_figure(F.flow_figure(summary, banner=fbanner), "Search and screening flow diagram; the same counts follow "
                                                         "as a table."))
    flow_tab = FL.flow_table(summary)
    add("<details><summary>Flow counts as a table</summary>"
        + _table([("section", "Section"), ("item", "Item"), ("count", "Count")], flow_tab.rows, numeric=["count"])
        + "</details>")
    add(_notes(summary["notes"]))
    add('<p class="note">PRISMA-style layout; no compliance with a reporting guideline is claimed.</p>')
    runs = conn.execute("SELECT id, provider, interface, mode, executed_query, started_at, retrieved_count,"
                        " provider_total, status, exploratory FROM search_run ORDER BY started_at, id").fetchall()
    add("<h3>Search runs</h3>")
    add(_table([("started_at", "Started (UTC)"), ("provider", "Provider"), ("mode", "Mode"),
                ("executed_query", "Query as executed"), ("retrieved_count", "Retrieved"),
                ("provider_total", "Provider total"), ("status", "Status"), ("exploratory", "Exploratory")],
               [{**dict(r), "exploratory": bool(r["exploratory"])} for r in runs], numeric=["retrieved_count", "provider_total"], wide=["executed_query"],
               caption="Mode 'fixture' marks bundled synthetic responses; 'live' marks provider responses.",
               empty="No search has been run in this project."))

    # full text
    add('<h2 id="fulltext">Full text</h2>')
    add(_table([("k", "Full-text availability"), ("n", "Reports")],
               [{"k": k, "n": v} for k, v in ft["by_availability"].items()], numeric=["n"],
               caption=f"{ft['reports']} report(s); {ft['reports_with_findings']} have findings."))
    if ft["documents_by_parse_status"]:
        add(_table([("k", "Parse status of the latest snapshot"), ("n", "Documents")],
                   [{"k": k, "n": v} for k, v in sorted(ft["documents_by_parse_status"].items())], numeric=["n"]))
    add(_notes(ft["notes"]))

    # maps
    add('<h2 id="maps">Evidence maps</h2>')
    add("<p>Where the project holds findings, by stage and by term. The maps show how much is recorded, not what "
        "it shows.</p>")
    for by, emap in emaps.items():
        add(f"<h3>{_e(emap['title'])}</h3>")
        add(_heatmap(emap))
    notes = list(dict.fromkeys(n for emap in emaps.values() for n in emap["notes"]))
    add(_notes(notes))

    # missing
    add('<h2 id="missing">Missing information</h2>')
    add(_figure(F.missing_figure(miss, banner=fbanner), "Stacked bars: for each field, the number of measurement "
                                                        "findings that record it and the recorded reason where "
                                                        "they do not; the same counts follow as a table."))
    cols = [("stage", "Stage"), ("field", "Field"), ("findings", "Findings"),
            *((s, s.replace("_", " ")) for s in MI.STATES)]
    add("<details><summary>Counts by stage and field as a table</summary>"
        + _table(cols, miss["rows"], numeric=["findings", *MI.STATES], empty="No measurement findings.")
        + "</details>")
    add(_notes(miss["notes"]))

    # modelling
    add('<h2 id="modeling">Modelling readiness</h2>')
    add(f"<p>{_e(inv['statement'])}</p>")
    add(_table([(c, c.replace("_", " ")) for c in inv["columns"][1:]], inv["rows"], numeric=inv["columns"][2:]))
    for dim, vals in inv["reviewer_assessments"].items():
        text = ", ".join(f"{k}: {v} paper(s)" for k, v in vals.items()) or "none recorded"
        add(f'<p>Reviewer assessments, {_e(dim.replace("_", " "))}: {_e(text)}</p>')
    add(_notes(inv["notes"]))

    # comparison sets
    add('<h2 id="comparison">Comparison sets</h2>')
    add(f"<p>{_e(comparison.STATEMENT)}</p>")
    if not sets:
        add('<p class="note">No comparison set has been saved.</p>')
    for s in sets:
        a = s["assessment"]
        ind = a.get("independence") or {}
        add(f"<h3>{_e(s['name'])}</h3>")
        add(f'<p><span class="status">{_e(s["classification"].replace("_", " "))}</span> · saved {_e(s["created_at"])} '
            f"by {_e(s['created_by'])} · {len(s['finding_ids'])} finding(s), at most "
            f"{_e(ind.get('independent_units_at_most'))} independent unit(s)"
            + ("" if s["is_current"] else " · <strong>out of date: "
               f"{len(s['changed_since_assessment'])} finding(s) changed after the assessment</strong>") + "</p>")
        if s.get("description"):
            add(f"<p>{_e(s['description'])}</p>")
        add(_table([("check", "Check"), ("level", "Level"), ("status", "Status"), ("detail", "Detail")],
                   a.get("checks", []), wide=["detail"], caption="Compatibility assessment as saved"))
        try:
            tab = comparison.table(conn, s["finding_ids"])
        except Exception as exc:  # noqa: BLE001 - a set whose findings were removed still shows its assessment
            add(f'<p class="note">The side-by-side table could not be built: {_e(exc)}</p>')
            continue
        add(_table([("citation", "Report"), ("independence_unit", "Independence unit"), ("outcome", "Outcome"),
                    ("system_context", "System"), ("as_stated", "As stated"), ("display_value", "Display value"),
                    ("display_low", "low"), ("display_high", "high"), ("display_unit", "Display unit"),
                    ("display_rule", "Rule"), ("uncertainty_literal", "Uncertainty as stated"),
                    ("sample_size", "n"), ("timepoint_literal", "Time point"), ("review_status", "Review"),
                    ("source_location", "Source location")], tab["rows"],
                   numeric=["display_value", "display_low", "display_high", "sample_size"],
                   caption="Current values of the findings, side by side"))
        add(f'<p class="note">{_e(tab["note"])}</p>')

    # validation
    add('<h2 id="validation">Validation</h2>')
    add(f"<p>{_e(val['statement'])}</p>")
    if val.get("run_id"):
        add(f"<p>Latest run {_e(val['created_at'])} by {_e(val['actor'])} (rules {_e(val['rules_version'])}): "
            f"{val['n_findings']} finding(s) checked, {val['n_issues']} issue(s) on {val['findings_with_issues']} "
            f"finding(s); errors {val['by_severity']['error']}, warnings {val['by_severity']['warning']}, "
            f"information {val['by_severity']['info']}.</p>")
        add(_table([("code", "Rule"), ("n", "Issues")], [{"code": k, "n": v} for k, v in val["by_code"].items()],
                   numeric=["n"], empty="The latest run found no issues."))

    # findings
    add('<h2 id="findings">Evidence table</h2>')
    shown = len(page.rows)
    add(f"<p>{shown} of {page.total} finding(s)" + (" matching the filter" if filtered else "")
        + (", ordered by report. The complete table is in the export files written with this report."
           if shown < page.total else ", ordered by report.") + "</p>")
    add(_table([("citation", "Report"), ("stages", "Stages"), ("kind", "Kind"),
                ("source_wording", "Source wording"), ("outcome", "Outcome"), ("system_context", "System"),
                ("as_stated", "Value as stated"), ("timepoint", "Time point"), ("review_status", "Review"),
                ("grounding_status", "Grounding"), ("source_location", "Source location"), ("finding_id", "ID")],
               _finding_rows(page.rows), wide=["source_wording", "citation"], ids=["finding_id"],
               empty="No findings are recorded."))
    add('<p class="note">Source wording is the text as written in the source (shortened where long). '
        "Review status 'verified' is the only status that records a reviewer's confirmation.</p>")

    # papers
    add('<h2 id="papers">Reports</h2>')
    ptab = T.papers_table(conn)
    add(f"<p>{min(len(ptab.rows), max_papers)} of {len(ptab.rows)} report record(s).</p>")
    add(_table([("citation", "Citation"), ("title", "Title"), ("year", "Year"), ("doi", "DOI"),
                ("resolution_status", "Identifier"), ("identity_status", "Identity"),
                ("fulltext_availability", "Full text"), ("screening_title_abstract", "Title/abstract screening"),
                ("screening_full_text", "Full-text screening"), ("n_findings", "Findings"),
                ("correction_retraction", "Corrections and retractions"), ("origin", "Origin")],
               ptab.rows[:max_papers], numeric=["n_findings"], wide=["title", "citation", "correction_retraction"]))
    add(f'<p class="note">{_e(ptab.note)}</p>')

    # inputs
    add('<h2 id="inputs">Inputs and audit trail</h2>')
    add("<p>SHA-256 hashes of the preserved inputs this report was made from. The files stay in the project data "
        "directory and are not part of the report.</p>")
    add(_table([("workbook_key", "Workbook"), ("version_no", "Version"), ("original_filename", "File name"),
                ("sha256", "SHA-256"), ("imported_at", "Imported (UTC)")], hashes["workbook_versions"],
               ids=["sha256"], empty="No workbook has been imported."))
    add(_table([("document_id", "Document"), ("original_filename", "File name"), ("format", "Format"),
                ("sha256", "SHA-256"), ("retrieved_at", "Stored (UTC)")], hashes["documents"][:max_papers],
               ids=["sha256", "document_id"], empty="No document is stored."))
    add(f"<p>Raw provider responses stored: {len(hashes['search_raw_responses'])} "
        f"(fixtures: {sum(1 for r in hashes['search_raw_responses'] if r['is_fixture'])}).</p>")
    add(f"<p>Audit log: {n_events} event(s); hash chain "
        + ("verified." if ok else f"<strong>broken at {_e(problem)}</strong>.") + "</p>")

    # limitations
    add('<h2 id="limits">Limitations</h2><ul class="plain">'
        + "".join(f"<li>{_e(x)}</li>" for x in LIMITATIONS) + "</ul>")
    add(f'<p class="note">{_e(W.banner(info))}</p>')
    add("</main></body></html>")
    return "".join(h)
