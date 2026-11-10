"""Offline demonstration of the complete workflow in a new SYNTHETIC project.

``run_demo`` is what ``geodesic demo`` runs. It generates every input it uses
(a fixture workbook, fixture search responses, a fixture PDF and a fixture XML
article - all invented, all tagged as synthetic), creates a project declared
synthetic, and walks through each workflow step by calling the same public
functions the CLI and the dashboard call. Nothing is fetched from a network.

Review actions - confirming a mapping, confirming the protocol, screening
decisions, verification of findings, groupings of terms - are judgements a
researcher makes. Here they are made by *simulated* reviewers
(``Actor(kind="simulated")``); the database records them as simulated, the
application only permits simulated actors in synthetic projects, and every
count that separates human from simulated decisions shows them as simulated.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable

from . import audit, comparison, dedup, protocol, query, review, screening, vocabulary
from .config import dump_yaml
from .documents import openaccess, store
from .errors import GeodesicError
from .extraction import candidates, derived, hierarchy, manual, validation
from .project import Project, create_project
from .reports import build
from .search import europepmc, fileimport
from .util import Actor

SIM_A = Actor("Simulated Reviewer 1", "simulated")
SIM_B = Actor("Simulated Reviewer 2", "simulated")
DEMO = Actor("geodesic-demo", "system")
ALL_CONFIRMED = {name: "confirmed" for name in review.REQUIRED_CHECKS}
TA, FT = "title_abstract", "full_text"
NOTE = "simulated review action of the offline demonstration"


def _finding(conn, literal: str, unit: str) -> str:
    row = conn.execute("SELECT id FROM finding WHERE value_literal = ? AND unit_original = ? AND"
                       " record_status = 'active' ORDER BY pk LIMIT 1", (literal, unit)).fetchone()
    if row is None:
        raise GeodesicError(f"The demonstration fixture holds no finding stating {literal} {unit}.")
    return row[0]


def _candidate(conn, snapshot_id: str, literal: str) -> dict[str, Any]:
    found = [c for c in candidates.list_candidates(conn, snapshot_id=snapshot_id, status=None, limit=500)
             if c["literal"] == literal]
    if len(found) != 1:
        raise GeodesicError(f"The demonstration fixture should hold one candidate {literal!r}; found {len(found)}.")
    return found[0]


def write_inputs(directory: Path) -> dict[str, Path]:
    """Generate the synthetic input files of the demonstration into ``directory``."""
    from .synthetic import documents as SD
    from .synthetic import search as SS
    from .synthetic.protocol import example_protocol
    from .synthetic.workbook import example_config, fixture_mapping, write_fixture_workbook
    from .workbook.mapping import write_mapping

    directory.mkdir(parents=True, exist_ok=True)
    (directory / "workbook_v1").mkdir(exist_ok=True)
    (directory / "workbook_v2").mkdir(exist_ok=True)
    out = {
        "config": directory / "project.yaml",
        "workbook_v1": write_fixture_workbook(directory / "workbook_v1" / "synthetic_workbook.xlsx", version=1),
        "workbook_v2": write_fixture_workbook(directory / "workbook_v2" / "synthetic_workbook.xlsx", version=2),
        "mapping": directory / "mapping.confirmed.yaml",
        "protocol": directory / "protocol.yaml",
        "search_fixture": SS.write_europepmc_fixture(directory / "search_fixture", page_size=4, variant=1),
        "ris": SS.write_ris_fixture(directory / "synthetic_search_export.ris"),
        "pdf": SD.write_pdf_fixture(directory / "synthetic_article.pdf"),
    }
    dump_yaml(example_config().model_dump(mode="json"), out["config"])
    write_mapping(fixture_mapping(confirmed=True), out["mapping"])
    out["protocol"].write_text(protocol.dump_protocol(example_protocol(authorize_automation=True)), encoding="utf-8")
    return out


def run_demo(directory: Path | None = None, *, keep_existing: bool = False, with_report: bool = True,
             echo: Callable[[str], None] = lambda _line: None) -> dict[str, Any]:
    """Run the whole workflow offline in a new synthetic project and return what was produced.

    ``directory`` receives ``inputs/`` (generated fixture files), ``project/`` (the project data directory)
    and ``report/``. It must not already hold a project unless ``keep_existing`` is set, in which case the
    existing project is opened and returned untouched.
    """
    from .synthetic import documents as SD
    from .synthetic.workbook import example_config
    from .workbook.importer import format_reconciliation, import_workbook
    from .workbook.mapping import load_mapping
    from .workbook.profiler import write_profile

    root = Path(directory) if directory else Path(tempfile.mkdtemp(prefix="geodesic_demo_"))
    project_dir = root / "project"
    if (project_dir / "geodesic.sqlite").exists():
        if keep_existing:
            return {"project_dir": project_dir, "report": None, "existing": True}
        raise GeodesicError(f"{project_dir} already holds a project. Choose another --dir (nothing is overwritten).")
    step_no = 0

    def step(title: str) -> None:
        nonlocal step_no
        step_no += 1
        echo(f"\n[{step_no:>2}] {title}")

    def say(text: str) -> None:
        for line in str(text).splitlines():
            echo("     " + line)

    echo("GEODESIC offline demonstration - SYNTHETIC DATA: generated fixtures, not research evidence.")
    echo("Review actions below are SIMULATED and recorded as simulated; no network is used.")

    step("Generate synthetic input files")
    inputs = write_inputs(root / "inputs")
    say(f"written to {root / 'inputs'}: a fixture workbook in two versions, fixture search responses, a fixture "
        "RIS export, a fixture PDF, a project configuration and a protocol file")

    step("Create a project declared synthetic")
    project: Project = create_project(project_dir, example_config(), "synthetic", DEMO)
    conn = project.conn
    say(f"{project_dir}  (data class: {project.data_class}; stages from the configuration: "
        + ", ".join(s.name for s in project.config.stages) + ")")

    step("Profile the workbook and write a starter mapping")
    profile_dir = root / "inputs" / "profile"
    written = write_profile(inputs["workbook_v1"], profile_dir,
                            [{"key": s.key, "name": s.name} for s in project.config.stages])
    starter = load_mapping(written["mapping"])
    say(f"profile and starter mapping written to {profile_dir}; every inferred entry is marked unconfirmed")
    try:
        import_workbook(project, inputs["workbook_v1"], starter, actor=DEMO)
        raise AssertionError("the importer accepted an unconfirmed mapping")
    except GeodesicError as exc:
        say("importing with the unconfirmed starter mapping is refused: " + str(exc).splitlines()[0][:160])

    step("Import the workbook through a confirmed mapping")
    say("(the bundled confirmed mapping of the fixture stands in for the researcher's review of the starter "
        "mapping - simulated)")
    first = import_workbook(project, inputs["workbook_v1"], load_mapping(inputs["mapping"]), actor=DEMO)
    say(format_reconciliation(first.stats))

    step("Load a protocol; it is provisional until confirmed")
    protocol.save_new_version(conn, protocol.load_protocol_file(inputs["protocol"]), SIM_A,
                              "synthetic example protocol of the demonstration")
    say(f"protocol {protocol.current(conn).label}")

    step("Exploratory literature search under the provisional protocol (fixture responses, not live)")
    search = europepmc.run_search(project, DEMO, query="synthetic", page_size=4, mode="fixture",
                                  fixture_dir=inputs["search_fixture"])
    from .search import runs

    run = runs.list_runs(conn, limit=1)[0]
    say(f"run {run['id']}: {runs.run_label(run)}")
    try:
        screening.decide(conn, conn.execute("SELECT id FROM paper ORDER BY pk LIMIT 1").fetchone()[0], TA, "include",
                         SIM_A, require_final=True)
    except GeodesicError as exc:
        say("a final eligibility decision is refused while the protocol is provisional: "
            + str(exc).splitlines()[0][:150])

    step("Confirm the protocol (simulated reviewer)")
    say(f"protocol {protocol.confirm(conn, SIM_A, project.data_class).label}")

    step("Import a search-result file (RIS fixture)")
    ris = fileimport.import_search_file(project, inputs["ris"], DEMO)
    say(f"{ris['run']['retrieved_count']} records read; "
        + ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in ris["links"].items())
        + "".join(f"; note: {n}" for n in ris["notes"]))

    step("Detect duplicates; group identifier matches; leave the rest for review")
    found = dedup.find_duplicates(conn, project.config.dedup)
    say("(detection also runs when records arrive; this run) " + ", ".join(f"{k.replace('_', ' ')}: {v}"
                                                                         for k, v in found.items()))
    grouped = dedup.confirm_identifier_candidates(conn, SIM_A, note=NOTE)
    say("identifier matches confirmed by the simulated reviewer: " + ", ".join(f"{k}: {v}" for k, v in grouped.items()))
    open_rel = dedup.relations(conn, status="candidate")
    say(f"{open_rel['total']} duplicate candidate(s) still await a reviewer")

    step("Title/abstract screening: suggestions, automated rule, two simulated reviewers")
    sug = screening.suggest(conn, TA)
    say(f"suggestions (drafts, never decisions): {sug['created']} created - "
        + ", ".join(f"{k}: {v}" for k, v in sug["by_suggestion"].items()))
    auto = screening.apply_automation(conn, TA)
    say(f"automated exclusions the confirmed protocol authorises: {auto['applied']} applied "
        f"({auto['skipped_rule_not_authorised']} suggestion(s) rest on rules it does not authorise and stay open)")
    pdf_paper = conn.execute("SELECT id FROM paper WHERE doi = ? AND origin = 'workbook'", (SD.PDF_DOI,)).fetchone()[0]
    xml_paper = conn.execute("SELECT id FROM paper WHERE pmcid = ?", (SD.XML_PMCID,)).fetchone()[0]
    queue = [r["id"] for r in screening.queue(conn, TA, limit=200)["rows"]]
    chosen = list(dict.fromkeys([pdf_paper, xml_paper, *queue]))[:8]
    decided = 0
    for pid in chosen:
        try:
            screening.decide(conn, pid, TA, "include", SIM_A, reason_kind="meets_criteria", reason_text=NOTE,
                             require_final=True)
            decided += 1
        except GeodesicError as exc:
            say(f"{pid}: not decided ({str(exc).splitlines()[0][:110]})")
    second = [p for p in chosen if p not in (pdf_paper, xml_paper)][:2]
    for i, pid in enumerate(second):
        try:
            if i == 0:
                screening.decide(conn, pid, TA, "include", SIM_B, reason_kind="meets_criteria", reason_text=NOTE)
            else:
                screening.decide(conn, pid, TA, "uncertain", SIM_B, reason_kind="insufficient_information",
                                 reason_text=NOTE)
        except GeodesicError as exc:
            say(f"{pid}: second reviewer not recorded ({str(exc).splitlines()[0][:110]})")
    say(f"{decided} paper(s) decided by {SIM_A.name}; {len(second)} also by {SIM_B.name}; "
        f"disagreements awaiting adjudication: {len(screening.conflicts(conn, TA))}")

    step("Obtain full text: a user-provided PDF and an open-access XML (fixture response, not live)")
    pdf = store.add_document(project, pdf_paper, inputs["pdf"], DEMO)
    xml = openaccess.fetch_open_access(project, xml_paper, DEMO, transport=SD.fulltext_transport())
    pdf_snap, xml_snap = pdf["snapshot"]["snapshot_id"], xml["snapshot"]["snapshot_id"]
    say(f"PDF  {pdf['document_id']}: {pdf['snapshot']['status']}, {pdf['snapshot']['n_units']} page(s) of text")
    say(f"XML  {xml['document_id']}: {xml['snapshot']['status']}, {xml['snapshot']['n_units']} section unit(s), "
        f"{xml['snapshot']['n_tables']} table(s), license: {xml.get('license') or 'not supplied'}")
    for pid in (pdf_paper, xml_paper):
        try:
            screening.decide(conn, pid, FT, "include", SIM_A, reason_kind="meets_criteria", reason_text=NOTE,
                             require_final=True)
        except GeodesicError as exc:
            say(f"{pid}: full-text decision not recorded ({str(exc).splitlines()[0][:110]})")

    step("Detect candidate values; promote two; extract one by hand; dismiss one")
    for snap in (pdf_snap, xml_snap):
        det = candidates.detect(project, snap, DEMO)
        say(f"snapshot {snap}: {det['candidates']} candidate values ({det['with_unit']} with a unit), "
            f"{det['parse_failures']} parse failures - candidates are not findings")
    f_release = candidates.promote(project, _candidate(conn, pdf_snap, "62 ± 5 %")["id"], SIM_A,
                                   changes={"outcome": "tracer released"}, stage_keys=["release"], note=NOTE)
    f_transit = candidates.promote(project, _candidate(conn, xml_snap, "42 ± 3 min")["id"], SIM_A,
                                   changes={"outcome": "transit time"}, stage_keys=["transit"], note=NOTE)
    loc = manual.build_location(conn, pdf_snap, unit_key="1",
                                quote="The release rate constant was approximately 0.04 1/min.")
    (prop,) = manual.propose_from_span(project, loc)
    f_rate = manual.create_finding(project, pdf_paper, SIM_A, location=loc, spans=prop["spans"],
                                   content={**prop["content"], "outcome": "release rate constant"},
                                   stage_keys=["release"], note=NOTE)
    candidates.dismiss(conn, [_candidate(conn, pdf_snap, "120")["id"]], SIM_A, note=NOTE)
    say(f"new unreviewed findings: {f_release['id']} (promoted, PDF page), {f_transit['id']} (promoted, XML "
        f"section), {f_rate['id']} (manual); each is anchored to its stored source text")

    step("Review: verify against the source, correct, relate (simulated reviewers)")
    a, b = _finding(conn, "12.5", "mg/L"), _finding(conn, "18", "mg/L")
    for fid in (f_release["id"], f_transit["id"], f_rate["id"], a, b):
        review.verify(conn, fid, SIM_B, ALL_CONFIRMED, note=NOTE, acknowledge_flags=True)
    edited = conn.execute("SELECT id FROM finding WHERE record_status = 'active' AND kind = 'measurement' AND"
                          " outcome IS NULL AND review_status != 'verified' ORDER BY pk LIMIT 1").fetchone()[0]
    review.edit(conn, edited, SIM_A, {"outcome": "tracer concentration"}, note=NOTE)
    review.relate(conn, f_release["id"], f_rate["id"], "dependent", SIM_A,
                  note="same run of the same synthetic experiment")
    say(f"5 findings verified by {SIM_B.name}; 1 corrected (new version, earlier version kept); "
        "2 related as dependent")

    step("Organise: study, experiment, stage assignment, term grouping")
    study = hierarchy.create_study(conn, SIM_A, "Synthetic stirred-tank study", paper_ids=[pdf_paper])
    exp = hierarchy.create_experiment(conn, SIM_A, pdf_paper, "Synthetic run series 1", study_id=study["id"])
    for fid in (f_release["id"], f_rate["id"]):
        hierarchy.assign(conn, fid, SIM_A, experiment_id=exp["id"], note=NOTE)
    review.assign_stages(conn, f_rate["id"], SIM_A, add=["transit"], note=NOTE)
    for term in ("Stirred source tank, 200 L", "Lined source tank"):
        vocabulary.set_term(conn, "system_context", term, "Source tank", SIM_A, note=NOTE)
    say("1 study, 1 experiment with 2 findings; 1 finding now belongs to two stages; 2 original terms grouped "
        "under 'Source tank' (the stored terms are unchanged)")

    step("Normalise a value with the explicit unit table; compare without pooling")
    norm = derived.normalize_finding(project, a, SIM_A, target_unit="µg/mL")
    say(f"normalised value {norm['id']}: {norm['output_value']:g} {norm['output_unit']} by rule {norm['rule_id']} "
        "(stored beside the original, which is unchanged)")
    # verification is on the current version; marking eligibility needs it
    comparison.set_eligibility(conn, [a, b], "quantitative_candidate", SIM_A, note=NOTE)
    saved = comparison.save_set(conn, "Synthetic concentration pair", [a, b], SIM_A,
                                description="two verified synthetic concentration findings")
    ind = saved["assessment"]["independence"]
    say(f"comparison set {saved['id']}: {saved['classification']}, cautions: "
        f"{', '.join(saved['assessment']['cautions']) or 'none'}; {ind['findings']} findings form at most "
        f"{ind['independent_units_at_most']} independent unit(s)")
    units = comparison.count_units(comparison.independence_units(conn), [f_release["id"], f_rate["id"]])
    say(f"the two findings of one experiment count as {units['independent_units_at_most']} independent unit, "
        "not two")
    screening.assess(conn, pdf_paper, "primary_relevance", "direct", SIM_A, NOTE)
    screening.assess(conn, pdf_paper, "mathematical_modeling", "limited", SIM_A, NOTE)

    step("Validate")
    val = validation.run_validation(project, DEMO)
    say(f"run {val['run_id']}: {val['n_findings']} findings checked, {val['n_issues']} issue(s) - "
        + ", ".join(f"{k}: {v}" for k, v in val["by_severity"].items()))
    say(val["statement"])

    step("Import a changed version of the workbook (the first version stays preserved)")
    second_import = import_workbook(project, inputs["workbook_v2"], load_mapping(inputs["mapping"]), actor=DEMO)
    fs = second_import.stats["findings"]
    say(f"workbook version {second_import.version_no}: "
        + ", ".join(f"{k.replace('_', ' ')} {fs[k]}" for k in ("added", "unchanged", "modified", "absent", "restored")))
    say(f"reviews marked stale because their source changed: {second_import.stats['reviews_marked_stale']}; "
        f"derived values marked stale: {second_import.stats['derived_values_marked_stale']}")

    report = None
    if with_report:
        step("Write the report directory (HTML report, figures, tables, manifest)")
        res = build.build_report(project, root / "report", actor=DEMO)
        report = res["html"]
        say(f"{len(res['files']) + 1} files in {res['directory']}; every file name starts with SYNTHETIC_")

    step("Check the audit trail")
    ok, n, bad = audit.verify_chain(conn)
    kinds = {r[0]: r[1] for r in conn.execute("SELECT actor_kind, COUNT(*) FROM audit_event GROUP BY actor_kind")}
    say(f"{n} audit events, hash chain {'intact' if ok else f'BROKEN at {bad}'}; by actor kind: "
        + ", ".join(f"{k}: {v}" for k, v in sorted(kinds.items())))
    counts = query.counts(conn)
    say(f"project now holds {counts['reports']} paper records, {counts['findings']} findings, "
        f"{counts['documents']} documents, {counts['search_runs']} search runs")
    flow = screening.summary(conn)
    kinds_ta = flow["stages"][TA]["current_decisions_by_decider"]
    say(f"title/abstract decisions - human: {kinds_ta['human']}, automated: {kinds_ta['automated']}, "
        f"simulated: {kinds_ta['simulated']}")
    out = {"project_dir": project_dir, "inputs": inputs, "report": report, "counts": counts,
           "audit_events": n, "audit_chain_ok": ok, "reconciliation": first.stats, "search": search,
           "steps": step_no, "existing": False}
    project.close()
    return out
