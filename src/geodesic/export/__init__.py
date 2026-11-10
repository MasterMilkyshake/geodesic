"""Exports: one function writes a consistent, self-describing export directory.

``export_tables`` is what ``geodesic export`` and the dashboard's export
controls call. Every export directory contains ``manifest.json`` with the
SHA-256 of each file written and of each input (workbook versions, documents),
so an export can be tied back to exactly the inputs it was made from.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .. import __version__, audit, query
from ..errors import GeodesicError
from ..project import Project, _version_controlled_ancestor, exportable_config
from ..util import SYSTEM, Actor, sha256_file, utc_now
from . import tables as T
from . import writers as W

TABLE_FORMATS = ("xlsx", "csv", "json", "ris")


def new_export_dir(project: Project, out_dir: Path | str | None, kind: str = "export") -> Path:
    """Directory for a new export. Real project data is never written into a version-controlled directory."""
    if out_dir is None:
        stamp = utc_now().replace(":", "").replace("-", "")[:15]
        out = project.exports_dir / f"{kind}_{stamp}"
        n = 1
        while out.exists():
            n += 1
            out = project.exports_dir / f"{kind}_{stamp}_{n}"
    else:
        out = Path(out_dir)
    if not project.is_synthetic:
        vcs = _version_controlled_ancestor(out)
        if vcs is not None:
            raise GeodesicError(
                f"Refusing to export real project data into {out}: {vcs} is a source-controlled or application "
                "source directory. Choose a directory outside source control.")
    out.mkdir(parents=True, exist_ok=True)
    return out


def write_table_file(project: Project, table: T.Table, dest: Path | str) -> Path:
    """Write one table as one CSV file. The file name carries the synthetic prefix where it applies."""
    dest = Path(dest)
    info = T.project_info(project)
    prefix = W.file_prefix(info)
    if prefix and not dest.name.startswith(prefix):
        dest = dest.with_name(prefix + dest.name)
    if not project.is_synthetic:
        vcs = _version_controlled_ancestor(dest.parent)
        if vcs is not None:
            raise GeodesicError(
                f"Refusing to write real project data into {dest.parent}: {vcs} is a source-controlled or "
                "application source directory. Choose a directory outside source control.")
    return W.write_csv(table, info, dest)


def input_hashes(project: Project) -> dict[str, list[dict[str, Any]]]:
    """Hashes of the preserved inputs the project's evidence was derived from."""
    conn = project.conn
    return {
        "workbook_versions": [dict(r) for r in conn.execute(
            "SELECT workbook_key, version_no, original_filename, sha256, size_bytes, imported_at"
            " FROM workbook_version ORDER BY workbook_key, version_no")],
        "documents": [dict(r) for r in conn.execute(
            "SELECT id AS document_id, paper_id, original_filename, format, sha256, size_bytes, retrieved_at"
            " FROM document ORDER BY retrieved_at, id")],
        "search_raw_responses": [dict(r) for r in conn.execute(
            "SELECT search_run_id, seq, sha256, size_bytes, is_fixture FROM search_raw_response"
            " ORDER BY search_run_id, seq")],
    }


def write_manifest(project: Project, out: Path, files: Iterable[Path], info: dict[str, Any],
                   extra: dict[str, Any] | None = None) -> Path:
    """Write ``manifest.json`` describing an export directory (file hashes, input hashes, data class)."""
    manifest = {
        "label": W.banner(info),
        "generated_at": utc_now(),
        "generated_by": f"GEODESIC {__version__}",
        "project": {"id": project.id, "name": project.name, "data_class": project.data_class},
        "protocol_version": info["protocol_version"],
        "protocol_confirmed": info["protocol_confirmed"],
        "counts": info["counts"],
        "files": [{"name": p.name, "sha256": sha256_file(p), "size_bytes": p.stat().st_size}
                  for p in sorted(files, key=lambda p: p.name)],
        "inputs": input_hashes(project),
        **(extra or {}),
    }
    path = out / f"{W.file_prefix(info)}manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def check_formats(formats: Iterable[str]) -> list[str]:
    formats = list(dict.fromkeys(formats))
    unknown = [f for f in formats if f not in TABLE_FORMATS]
    if unknown:
        raise GeodesicError(f"Unknown export format(s) {unknown}; choose from {list(TABLE_FORMATS)}")
    return formats


def write_table_files(project: Project, out: Path, formats: list[str], tabs: list[T.Table],
                      info: dict[str, Any]) -> list[Path]:
    """Write ``tabs`` into ``out`` in each of ``formats`` plus the exportable project configuration."""
    prefix = W.file_prefix(info)
    files: list[Path] = []
    if "xlsx" in formats:
        files.append(W.write_excel(tabs, info, out / f"{prefix}evidence.xlsx"))
    if "csv" in formats:
        for t in tabs:
            files.append(W.write_csv(t, info, out / f"{prefix}{t.name}.csv"))
    if "json" in formats:
        files.append(W.write_json(tabs, info, out / f"{prefix}evidence.json"))
    if "ris" in formats:
        papers_tab = next(t for t in tabs if t.name == "papers")
        files.append(W.write_ris(papers_tab.rows, info, out / f"{prefix}references.ris"))
    cfg = out / f"{prefix}project_configuration.json"
    cfg.write_text(json.dumps(exportable_config(project), ensure_ascii=False, indent=1), encoding="utf-8")
    files.append(cfg)
    return files


def export_tables(project: Project, out_dir: Path | str | None = None, formats: Iterable[str] = ("xlsx", "csv"),
                  flt: query.FindingFilter | None = None, actor: Actor = SYSTEM) -> dict[str, Any]:
    """Write the evidence tables in the requested formats and a manifest.

    Returns ``{"directory", "files", "manifest", "rows"}``. ``flt`` restricts
    the findings (and their source locations, and the summaries counted over
    findings); the default exports all active findings.
    """
    formats = check_formats(formats)
    out = new_export_dir(project, out_dir)
    info = T.project_info(project)
    tabs = T.standard_tables(project, flt)
    files = write_table_files(project, out, formats, tabs, info)
    rows = {t.name: len(t.rows) for t in tabs}
    manifest = write_manifest(project, out, files, info, {"rows": rows, "formats": formats})
    audit.record(project.conn, actor, "export.created", "project", project.id,
                 {"directory": str(out), "formats": formats, "files": [p.name for p in files], "rows": rows})
    return {"directory": out, "files": files, "manifest": manifest, "rows": rows}
