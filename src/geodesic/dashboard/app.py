"""GEODESIC dashboard (Streamlit).

Start it with ``geodesic dashboard --project DIR`` (which runs
``streamlit run app.py -- --project DIR``), or set ``GEODESIC_PROJECT``.
The dashboard is a local, single-user front end: each page calls the same
public functions as the CLI and holds no logic of its own. The project is
opened at the start of every script run and closed at its end.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st

from geodesic import config, protocol
from geodesic.dashboard import pages_analysis, pages_evidence, pages_output, pages_search, pages_setup, ui
from geodesic.errors import GeodesicError
from geodesic.export import tables as T
from geodesic.export import writers as W
from geodesic.project import open_project

ENV_PROJECT = "GEODESIC_PROJECT"
ENV_REVIEWER = "GEODESIC_REVIEWER"

# Workflow order. Every implemented workflow step is one page.
PAGES = {
    "Overview": pages_setup.overview,
    "1 Protocol (research scope)": pages_setup.protocol_page,
    "2 Workbook import": pages_setup.workbook_page,
    "3 Literature search": pages_search.search_page,
    "4 Duplicates": pages_search.duplicates_page,
    "5 Screening": pages_search.screening_page,
    "6 Documents (full text)": pages_evidence.documents_page,
    "7 Extraction": pages_evidence.extraction_page,
    "8 Review findings": pages_evidence.review_page,
    "9 Organise": pages_analysis.organise_page,
    "10 Compare": pages_analysis.compare_page,
    "11 Evidence maps": pages_analysis.maps_page,
    "12 Validation": pages_analysis.validation_page,
    "13 Reports and exports": pages_output.reports_page,
    "Audit trail": pages_output.audit_page,
}


def project_dir_from_argv(argv: list[str] | None = None) -> Path | None:
    """``--project DIR`` after the ``--`` of ``streamlit run``, else the ``GEODESIC_PROJECT`` variable."""
    argv = sys.argv[1:] if argv is None else argv
    for i, arg in enumerate(argv):
        if arg == "--project" and i + 1 < len(argv):
            return Path(argv[i + 1]).expanduser()
        if arg.startswith("--project="):
            return Path(arg.split("=", 1)[1]).expanduser()
    value = os.environ.get(ENV_PROJECT)
    return Path(value).expanduser() if value else None


def default_reviewer(project_root: Path) -> str:
    try:
        return config.resolve_reviewer(os.environ.get(ENV_REVIEWER), project_root)
    except GeodesicError:
        return ""


def main() -> None:
    st.set_page_config(page_title="GEODESIC", layout="wide")
    project_dir = project_dir_from_argv()
    if project_dir is None:
        st.error("No project was given. Start the dashboard with:  geodesic dashboard --project DIR")
        return
    try:
        project = open_project(project_dir)
    except GeodesicError as exc:
        st.error(str(exc))
        return
    try:
        info = T.project_info(project)
        pv = protocol.current(project.conn)
        with st.sidebar:
            st.title("GEODESIC")
            if project.is_synthetic:
                st.warning("SYNTHETIC project: generated fixture data, not research evidence.")
            else:
                st.info("Real project. Its data stay in the project data directory.")
            st.caption(f"Project: {project.name}")
            st.caption(f"Data directory: {project.root}")
            st.caption(f"Protocol {pv.label}")
            reviewer_name = st.text_input("Reviewer (your name)", value=default_reviewer(project.root),
                                          key="reviewer_name",
                                          help="Review actions are recorded under this name.")
            page = st.radio("Workflow", list(PAGES), key="page")
        st.caption(W.banner(info))
        if not pv.is_confirmed:
            st.warning(f"Protocol {pv.label}: eligibility decisions cannot be finalised and automated exclusions "
                       "are disabled until the protocol is confirmed. Searches run now are labelled exploratory.")
        ui.show_flash()
        try:
            PAGES[page](ui.Ctx(project, reviewer_name))
        except GeodesicError as exc:
            # a refusal raised outside ui.attempt (e.g. a review action without a reviewer name)
            st.error(str(exc))
    finally:
        project.close()


if __name__ == "__main__":
    main()
