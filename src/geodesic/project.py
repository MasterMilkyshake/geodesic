"""Project data directories.

One project = one directory holding one SQLite database plus preserved source
files. The directory is separate from the source tree; a *real* project is
refused inside any version-controlled directory.

Layout::

    <project>/
      project.yaml            exportable project configuration (stages, parser, dedup, search)
      local.yaml              optional, per-machine (reviewer, contact e-mail); never exported
      geodesic.sqlite         linked evidence database
      sources/workbooks/      byte-identical workbook copies, named by SHA-256
      sources/documents/      byte-identical document copies, named by SHA-256
      sources/search_files/   byte-identical imported search-result files
      raw_responses/          stored provider responses (credentials excluded)
      backups/                automatic pre-migration database backups
      exports/                generated exports
      logs/                   structured logs
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import __version__, audit, protocol
from .config import ProjectConfig, dump_yaml, load_project_config
from .db import connect, migrate, refresh_statistics, transaction
from .errors import ConfigError, GeodesicError, ProvenanceError
from .logging_utils import configure_logging
from .provenance import DATA_CLASSES
from .util import Actor, SYSTEM, canonical_json, new_id, sha256_file, slugify, utc_now

log = logging.getLogger("geodesic.project")

DB_NAME = "geodesic.sqlite"
CONFIG_NAME = "project.yaml"


@dataclass
class Project:
    """An open project: paths, connection, immutable identity and configuration."""

    root: Path
    conn: sqlite3.Connection
    id: str
    name: str
    slug: str
    data_class: str
    config: ProjectConfig

    # ---- paths
    @property
    def db_path(self) -> Path:
        return self.root / DB_NAME

    @property
    def workbooks_dir(self) -> Path:
        return self.root / "sources" / "workbooks"

    @property
    def documents_dir(self) -> Path:
        return self.root / "sources" / "documents"

    @property
    def search_files_dir(self) -> Path:
        return self.root / "sources" / "search_files"

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw_responses"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def is_synthetic(self) -> bool:
        return self.data_class == "synthetic"

    def rel(self, path: Path) -> str:
        """Path relative to the project root (what is stored in the database)."""
        return str(Path(path).resolve().relative_to(self.root.resolve()))

    def abs(self, rel_path: str) -> Path:
        return self.root / rel_path

    def close(self) -> None:
        refresh_statistics(self.conn)
        self.conn.close()

    def __enter__(self) -> "Project":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _version_controlled_ancestor(path: Path) -> Path | None:
    p = path.resolve()
    for candidate in [p, *p.parents]:
        if (candidate / ".git").exists() or (candidate / ".hg").exists() or (candidate / ".svn").exists():
            return candidate
        if (candidate / "src" / "geodesic" / "__init__.py").exists():
            return candidate
    return None


def preserve_source_file(src: Path, dest_dir: Path) -> tuple[Path, str, int]:
    """Copy ``src`` byte-for-byte into ``dest_dir`` as ``<sha256><suffix>`` and verify the copy.

    The input file is opened read-only and is never modified. Returns
    ``(stored_path, sha256, size_bytes)``. An existing identical copy is reused.
    """
    src = Path(src)
    if not src.is_file():
        raise GeodesicError(f"Input file not found: {src}")
    digest = sha256_file(src)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{digest}{src.suffix.lower()}"
    if not dest.exists():
        tmp = dest.with_suffix(dest.suffix + ".part")
        shutil.copyfile(src, tmp)
        if sha256_file(tmp) != digest:
            tmp.unlink(missing_ok=True)
            raise GeodesicError(f"Copy of {src} did not match its SHA-256; nothing was imported.")
        tmp.replace(dest)
        dest.chmod(0o444)
    elif sha256_file(dest) != digest:
        raise GeodesicError(
            f"Preserved copy {dest} no longer matches its SHA-256 name; the project data directory was altered."
        )
    return dest, digest, src.stat().st_size


def create_project(root: Path | str, config: ProjectConfig, data_class: str, actor: Actor = SYSTEM) -> Project:
    """Create a project directory and database. ``data_class`` is immutable afterwards."""
    root = Path(root).expanduser()
    if data_class not in DATA_CLASSES:
        raise ProvenanceError(f"data_class must be one of {DATA_CLASSES}; got {data_class!r}.")
    if (root / DB_NAME).exists():
        raise GeodesicError(f"A project database already exists in {root}. Open it instead of re-creating it.")
    if data_class == "real":
        vc = _version_controlled_ancestor(root)
        if vc is not None:
            raise ProvenanceError(
                f"Refusing to create a real project inside a version-controlled or source directory ({vc}). "
                "Real project data must live in a separate local data directory, e.g. under $GEODESIC_HOME."
            )
    root.mkdir(parents=True, exist_ok=True)
    for sub in ("sources/workbooks", "sources/documents", "sources/search_files", "raw_responses", "backups",
                "exports", "logs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    dump_yaml(config.model_dump(mode="json"), root / CONFIG_NAME)
    conn = connect(root / DB_NAME)
    migrate(conn, root / "backups")
    pid = new_id("prj")
    slug = slugify(config.name)
    with transaction(conn):
        conn.execute(
            "INSERT INTO project (id, name, slug, data_class, created_at, created_by, app_version)"
            " VALUES (?,?,?,?,?,?,?)",
            (pid, config.name, slug, data_class, utc_now(), actor.name, __version__),
        )
        audit.record(conn, actor, "project.create", "project", pid,
                     {"name": config.name, "data_class": data_class, "app_version": __version__})
    protocol.initialise(conn, actor)
    project = Project(root=root, conn=conn, id=pid, name=config.name, slug=slug, data_class=data_class, config=config)
    sync_stages(project, actor)
    configure_logging(project.logs_dir / "geodesic.log")
    log.info("project created", extra={"project_id": pid, "data_class": data_class})
    return project


def open_project(root: Path | str) -> Project:
    """Open an existing project, applying pending migrations (with a pre-migration backup)."""
    root = Path(root).expanduser()
    db_path = root / DB_NAME
    if not db_path.is_file():
        raise GeodesicError(
            f"No GEODESIC project found in {root} (missing {DB_NAME}). "
            "Create one with: geodesic init --project <dir> --config project.yaml --data-class synthetic|real"
        )
    conn = connect(db_path)
    migrate(conn, root / "backups")
    refresh_statistics(conn)
    row = conn.execute("SELECT * FROM project").fetchone()
    if row is None:
        raise GeodesicError(f"{db_path} has no project record; it was not created by 'geodesic init'.")
    config = load_project_config(root / CONFIG_NAME)
    configure_logging(root / "logs" / "geodesic.log")
    return Project(root=root, conn=conn, id=row["id"], name=row["name"], slug=row["slug"],
                   data_class=row["data_class"], config=config)


def sync_stages(project: Project, actor: Actor = SYSTEM) -> dict[str, int]:
    """Load stage names and definitions from ``project.yaml`` into the database.

    Stages are never hard-coded and never deleted: a stage missing from the
    configuration is deactivated, keeping existing assignments intact.
    """
    conn = project.conn
    now = utc_now()
    stats = {"added": 0, "updated": 0, "deactivated": 0}
    with transaction(conn):
        existing = {r["key"]: r for r in conn.execute("SELECT * FROM stage")}
        seen = set()
        for pos, st in enumerate(project.config.stages):
            seen.add(st.key)
            kw = canonical_json(st.keywords)
            row = existing.get(st.key)
            if row is None:
                sid = new_id("stg")
                conn.execute(
                    "INSERT INTO stage (id, key, name, definition, keywords_json, position, active, created_at,"
                    " updated_at) VALUES (?,?,?,?,?,?,1,?,?)",
                    (sid, st.key, st.name, st.definition, kw, pos, now, now),
                )
                audit.record(conn, actor, "stage.add", "stage", sid, {"key": st.key, "name": st.name})
                stats["added"] += 1
            elif (row["name"], row["definition"], row["keywords_json"], row["position"], row["active"]) != (
                    st.name, st.definition, kw, pos, 1):
                conn.execute(
                    "UPDATE stage SET name = ?, definition = ?, keywords_json = ?, position = ?, active = 1,"
                    " updated_at = ? WHERE id = ?",
                    (st.name, st.definition, kw, pos, now, row["id"]),
                )
                audit.record(conn, actor, "stage.update", "stage", row["id"],
                             {"key": st.key, "before": {"name": row["name"], "definition": row["definition"]},
                              "after": {"name": st.name, "definition": st.definition}})
                stats["updated"] += 1
        for key, row in existing.items():
            if key not in seen and row["active"]:
                conn.execute("UPDATE stage SET active = 0, updated_at = ? WHERE id = ?", (now, row["id"]))
                audit.record(conn, actor, "stage.deactivate", "stage", row["id"], {"key": key})
                stats["deactivated"] += 1
    return stats


def reload_config(project: Project, actor: Actor = SYSTEM) -> dict[str, int]:
    """Re-read ``project.yaml`` after the researcher edited it and sync stages."""
    project.config = load_project_config(project.root / CONFIG_NAME)
    return sync_stages(project, actor)


def stages(conn: sqlite3.Connection, active_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM stage" + (" WHERE active = 1" if active_only else "") + " ORDER BY position"
    return [dict(r) for r in conn.execute(sql)]


def stage_by_key(conn: sqlite3.Connection) -> dict[str, dict]:
    return {s["key"]: s for s in stages(conn, active_only=False)}


def exportable_config(project: Project) -> dict:
    """Project configuration snapshot for export. Excludes local settings and credentials."""
    pv = protocol.current(project.conn)
    vocab = [dict(r) for r in project.conn.execute(
        "SELECT field, original_term, normalized_term, note FROM vocabulary_term WHERE active = 1"
        " ORDER BY field, original_term")]
    mappings = [dict(r) for r in project.conn.execute(
        "SELECT id AS import_run_id, mapping_sha256, mapping_json FROM import_run"
        " WHERE kind = 'workbook' AND status = 'complete' ORDER BY started_at")]
    return {
        "project": {"id": project.id, "name": project.name, "data_class": project.data_class},
        "app_version": __version__,
        "project_config": project.config.model_dump(mode="json"),
        "protocol": {"version": pv.version, "status": pv.status, "content_sha256": pv.content_sha256,
                     "content": pv.protocol.model_dump(mode="json")},
        "vocabulary": vocab,
        "workbook_mappings": mappings,
    }


def load_config_file(path: Path | str) -> ProjectConfig:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"Project configuration file not found: {p}")
    return load_project_config(p)
