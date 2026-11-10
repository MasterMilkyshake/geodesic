"""Figures: drawn from the same dictionaries the CLI prints, labelled, and written as SVG, PDF and PNG."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from geodesic import query, screening
from geodesic.errors import ValidationError
from geodesic.reports import figures, maps, missing

BANNER = "SYNTHETIC DATA — generated fixtures, not real evidence"


def test_evidence_map_figure_states_the_counts_of_the_map(imported):
    project, _ = imported
    emap = maps.evidence_map(project.conn, "system_context")
    fig = figures.evidence_map_figure(emap, banner=BANNER)
    assert fig.texts[0] == BANNER
    assert emap["title"] in fig.texts
    for row in emap["rows"]:
        assert row["label"] in fig.texts
    # every non-empty cell is printed with its count, and the grand total appears
    printed = [t for t in fig.texts if t.isdigit()]
    expected = [str(m["findings"]) for cols in emap["cells"].values() for m in cols.values()]
    for value in set(expected):
        assert printed.count(value) >= expected.count(value)
    assert str(emap["total"]["findings"]) in printed
    # another metric of the same map draws that metric's numbers
    reports = figures.evidence_map_figure(emap, "reports")
    assert str(emap["total"]["reports"]) in reports.texts
    assert BANNER not in reports.texts
    with pytest.raises(ValidationError):
        figures.evidence_map_figure(emap, "best")


def test_figures_are_written_as_svg_pdf_and_png_at_300_dpi(imported, tmp_path):
    project, _ = imported
    fig = figures.evidence_map_figure(maps.evidence_map(project.conn, "outcome"), banner=BANNER)
    paths = fig.save(tmp_path / "figs" / "map_outcome")
    assert [p.name for p in paths] == ["map_outcome.svg", "map_outcome.pdf", "map_outcome.png"]
    svg = paths[0].read_text(encoding="utf-8")
    assert svg.lstrip().startswith("<?xml") and "<svg" in svg
    assert "Evidence map: stage by outcome" in svg            # title metadata; text itself is drawn as outlines
    assert paths[1].read_bytes().startswith(b"%PDF-")
    with Image.open(paths[2]) as im:
        assert min(im.info["dpi"]) >= 300
        assert abs(im.size[0] - fig.width * 300 / 96) <= 1 and abs(im.size[1] - fig.height * 300 / 96) <= 1
    # higher resolution is allowed, lower is refused
    with Image.open(io.BytesIO(fig.render("png", dpi=600))) as im:
        assert min(im.info["dpi"]) >= 600
    with pytest.raises(ValidationError, match="300 DPI"):
        fig.render("png", dpi=150)
    with pytest.raises(ValidationError, match="format"):
        fig.render("gif")


def test_svg_output_is_reproducible(imported):
    project, _ = imported
    emap = maps.evidence_map(project.conn, "system_context")
    first = figures.evidence_map_figure(emap, banner=BANNER).svg()
    second = figures.evidence_map_figure(emap, banner=BANNER).svg()
    assert first == second


def test_missing_information_figure_uses_the_summary_counts(imported):
    project, _ = imported
    summary = missing.missing_information(project.conn)
    fig = figures.missing_figure(summary, banner=BANNER)
    assert fig.texts[0] == BANNER
    rows = [r for r in summary["rows"] if r["stage_key"] == missing.ALL_STAGES]
    for r in rows:
        assert r["field"].replace("_", " ") in fig.texts
        assert str(r["findings"]) in fig.texts
    for label in figures.STATE_LABELS.values():              # a legend entry for every state, also the unused ones
        assert label in fig.texts
    one_stage = figures.missing_figure(summary, "release")
    assert any("Release" in t for t in one_stage.texts)
    # a stage without measurement findings draws an empty, still labelled figure
    assert figures.missing_figure(summary, "no-such-stage", banner=BANNER).render("svg")


def test_flow_figure_repeats_the_screening_summary(corpus):
    summary = screening.summary(corpus.conn)
    fig = figures.flow_figure(summary, banner=BANNER)
    ident = summary["identification"]
    text = "\n".join(fig.texts)
    assert f"Paper records in the project: {ident['paper_records']}" in text
    assert f"Distinct records after grouping: {ident['distinct_records_after_grouping']}" in text
    assert f"records eligible: {summary['stages']['title_abstract']['eligible_for_stage']}" in text
    assert "PROVISIONAL" in text                              # the protocol of a new project is not confirmed
    assert "no compliance with a reporting guideline is claimed" in text.replace("\n", " ")


def test_standard_figures_cover_both_maps_missing_information_and_flow(corpus):
    figs = figures.standard_figures(corpus.conn, banner=BANNER)
    assert tuple(figs) == figures.STANDARD_FIGURES
    assert all(f.texts[0] == BANNER for f in figs.values())
    narrowed = figures.standard_figures(corpus.conn, flt=query.FindingFilter(kinds=["measurement"]))
    total = maps.evidence_map(corpus.conn, "system_context", flt=query.FindingFilter(kinds=["measurement"]))["total"]
    assert str(total["findings"]) in narrowed["evidence_map_system_context"].texts


def test_empty_project_still_draws(project):
    figs = figures.standard_figures(project.conn, banner=BANNER)
    for fig in figs.values():
        assert fig.render("svg")
