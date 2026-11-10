"""Build a report directory: HTML report, figures, tables and a manifest.

``build_report`` is the one function behind ``geodesic report`` and the
dashboard's report button. Everything it writes is produced by the public
functions the rest of the application uses, so a report cannot disagree with
the CLI or the dashboard about a count.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .. import audit, query
from ..errors import GeodesicError
from ..export import check_formats, new_export_dir, write_manifest, write_table_files
from ..export import tables as T
from ..export import writers as W
from ..project import Project
from ..util import SYSTEM, Actor
from . import figures as F
from . import html as H

PARTS = ("html", "figures", "tables")


def write_figures(project: Project, out: Path, *, flt: query.FindingFilter | None = None,
                  formats: Iterable[str] = F.FORMATS, dpi: int = 300, info: dict[str, Any] | None = None) -> list[Path]:
    """Write the standard figures into ``out`` as ``[SYNTHETIC_]figure_<name>.<format>``."""
    formats = list(dict.fromkeys(formats))
    unknown = [f for f in formats if f not in F.FORMATS]
    if unknown:
        raise GeodesicError(f"Unknown figure format(s) {unknown}; choose from {list(F.FORMATS)}")
    info = info or T.project_info(project)
    prefix = W.file_prefix(info)
    files: list[Path] = []
    for name, canvas in F.standard_figures(project.conn, flt=flt, banner=H.figure_banner(info)).items():
        files += canvas.save(out / f"{prefix}figure_{name}", formats, dpi)
    return files


def build_report(project: Project, out_dir: Path | str | None = None, *, flt: query.FindingFilter | None = None,
                 parts: Iterable[str] = PARTS, table_formats: Iterable[str] = ("xlsx", "csv", "json", "ris"),
                 figure_formats: Iterable[str] = F.FORMATS, dpi: int = 300, title: str | None = None,
                 actor: Actor = SYSTEM) -> dict[str, Any]:
    """Write a report directory and return ``{"directory", "files", "manifest", "html", "rows"}``."""
    parts = list(dict.fromkeys(parts))
    unknown = [p for p in parts if p not in PARTS]
    if unknown or not parts:
        raise GeodesicError(f"Report parts must be chosen from {list(PARTS)}; got {parts}")
    table_formats = check_formats(table_formats)
    out = new_export_dir(project, out_dir, "report")
    info = T.project_info(project)
    prefix = W.file_prefix(info)
    files: list[Path] = []
    html_path = None
    rows: dict[str, int] = {}
    if "html" in parts:
        html_path = out / f"{prefix}report.html"
        html_path.write_text(H.render_report(project, flt=flt, title=title), encoding="utf-8")
        files.append(html_path)
    if "figures" in parts:
        files += write_figures(project, out, flt=flt, formats=figure_formats, dpi=dpi, info=info)
    if "tables" in parts:
        tabs = T.standard_tables(project, flt)
        files += write_table_files(project, out, table_formats, tabs, info)
        rows = {t.name: len(t.rows) for t in tabs}
    manifest = write_manifest(project, out, files, info, {
        "kind": "report", "parts": parts, "rows": rows,
        "figure_resolution_dpi": dpi if "figures" in parts and "png" in figure_formats else None})
    audit.record(project.conn, actor, "report.created", "project", project.id,
                 {"directory": str(out), "parts": parts, "files": [p.name for p in files], "rows": rows})
    return {"directory": out, "files": files, "manifest": manifest, "html": html_path, "rows": rows}
