"""Database integrity: no hard deletes, append-only history, immutable columns, migrations, audit chain,
stage configuration, reviewer identity and credential redaction."""

from __future__ import annotations

import json
import logging
import sqlite3
import types

import pytest
import yaml

from conftest import OPERATOR
from geodesic import audit, db
from geodesic.config import load_local_settings, read_credential, resolve_reviewer
from geodesic.db import schema_v1
from geodesic.errors import ConfigError, GeodesicError
from geodesic.logging_utils import REDACTED, redact, redact_headers, redact_params
from geodesic.project import open_project, reload_config, stages


# ------------------------------------------------------------------ no deletes, append-only, immutable
def test_hard_deletes_are_refused_on_every_record_table(imported):
    project, _ = imported
    conn = project.conn
    triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    all_tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        " AND name NOT LIKE '%_fts%'")}
    # every table is protected, including stored validation runs
    unprotected = {t for t in all_tables if f"{t}_no_delete" not in triggers}
    assert not unprotected, unprotected
    populated = 0
    for table in schema_v1.NO_DELETE_TABLES:
        if conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0:
            continue                                   # row-level triggers only fire when there is a row
        populated += 1
        with pytest.raises(sqlite3.DatabaseError, match="hard delete is not permitted"):
            conn.execute(f"DELETE FROM {table}")
    assert populated >= 15
    assert conn.execute("SELECT COUNT(*) FROM finding").fetchone()[0] == 32
    assert conn.execute("SELECT COUNT(*) FROM paper").fetchone()[0] == 8


def test_history_tables_are_append_only(imported):
    project, _ = imported
    conn = project.conn
    with pytest.raises(sqlite3.DatabaseError, match="audit_event is append-only"):
        conn.execute("UPDATE audit_event SET actor = 'someone else'")
    with pytest.raises(sqlite3.DatabaseError, match="finding_version is append-only"):
        conn.execute("UPDATE finding_version SET snapshot_json = '{}'")
    with pytest.raises(sqlite3.DatabaseError, match="source_cell is append-only"):
        conn.execute("UPDATE source_cell SET raw_text = 'changed'")


def test_identity_and_provenance_columns_are_immutable(imported):
    project, _ = imported
    conn = project.conn
    for sql in ("UPDATE project SET data_class = 'real'",
                "UPDATE workbook_version SET sha256 = 'x'",
                "UPDATE workbook_version SET declared_data_class = 'real'",
                "UPDATE protocol_version SET content_json = '{}'"):
        with pytest.raises(sqlite3.DatabaseError, match="immutable column"):
            conn.execute(sql)
    # timestamps are UTC with an explicit offset
    for (ts,) in conn.execute("SELECT created_at FROM finding LIMIT 5"):
        assert ts.endswith("+00:00")
    assert conn.execute("SELECT imported_at FROM workbook_version").fetchone()[0].endswith("+00:00")


# ------------------------------------------------------------------ audit chain
def test_audit_chain_detects_tampering(imported):
    project, _ = imported
    conn = project.conn
    ok, n, problem = audit.verify_chain(conn)
    assert ok and problem is None and n == conn.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0]
    # simulate an edit made outside the application (the protective trigger has to be removed first)
    conn.execute("DROP TRIGGER audit_event_no_update")
    conn.execute("UPDATE audit_event SET details_json = '{\"tampered\":true}' WHERE seq = 2")
    ok, _, problem = audit.verify_chain(conn)
    assert not ok and problem is not None and "2" in problem


def test_audit_events_record_actor_kind_and_time(imported):
    project, _ = imported
    events = audit.history(project.conn, limit=500)
    assert all(e["ts"].endswith("+00:00") for e in events)
    assert {e["actor_kind"] for e in events} <= {"human", "system", "automated", "simulated"}
    first = audit.history(project.conn, action_prefix="project.create")
    assert len(first) == 1 and first[0]["actor"] == OPERATOR.name
    assert json.loads(first[0]["details_json"])["data_class"] == "synthetic"


# ------------------------------------------------------------------ migrations
def _fake_migration(version=2, fail=False):
    mod = types.ModuleType(f"fake_v{version}")
    mod.VERSION = version
    mod.DESCRIPTION = "test migration"

    def upgrade(conn):
        conn.execute("CREATE TABLE migration_probe (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO migration_probe (note) VALUES ('applied')")
        if fail:
            raise RuntimeError("migration failed half-way")

    mod.upgrade = upgrade
    return mod


def test_migration_writes_a_backup_before_changing_an_existing_database(imported):
    project, _ = imported
    conn = project.conn
    assert db.schema_version(conn) == 1
    assert db.migrate(conn, project.backups_dir) == []                      # nothing pending, no backup
    assert not project.backups_dir.exists() or not any(project.backups_dir.iterdir())

    applied = db.migrate(conn, project.backups_dir, migrations=[schema_v1, _fake_migration()])
    assert applied == [2] and db.schema_version(conn) == 2
    backups = list(project.backups_dir.glob("*pre-migration-v1*"))
    assert len(backups) == 1
    old = sqlite3.connect(backups[0])
    try:
        assert old.execute("PRAGMA user_version").fetchone()[0] == 1       # the backup is the pre-migration state
        assert old.execute("SELECT COUNT(*) FROM finding").fetchone()[0] == 32
        assert old.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'migration_probe'").fetchone()[0] == 0
    finally:
        old.close()
    assert conn.execute("SELECT note FROM migration_probe").fetchone()[0] == "applied"


def test_failed_migration_rolls_back_and_keeps_the_backup(imported):
    project, _ = imported
    conn = project.conn
    with pytest.raises(RuntimeError, match="half-way"):
        db.migrate(conn, project.backups_dir, migrations=[schema_v1, _fake_migration(fail=True)])
    assert db.schema_version(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'migration_probe'").fetchone()[0] == 0
    assert len(list(project.backups_dir.glob("*pre-migration-v1*"))) == 1
    assert conn.execute("SELECT COUNT(*) FROM finding").fetchone()[0] == 32


def test_database_from_a_newer_version_is_refused(project):
    root = project.root
    project.conn.execute("PRAGMA user_version = 99")
    project.close()
    with pytest.raises(GeodesicError, match="newer than this application supports"):
        open_project(root)


def test_opening_a_directory_without_a_project_gives_an_actionable_error(tmp_path):
    with pytest.raises(GeodesicError, match="geodesic init"):
        open_project(tmp_path / "nothing-here")


# ------------------------------------------------------------------ stages come from configuration
def test_stages_are_loaded_from_configuration_and_never_deleted(imported):
    project, _ = imported
    conn = project.conn
    assert [s["key"] for s in stages(conn)] == ["release", "transit", "retention", "discharge"]
    cfg_path = project.root / "project.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg["stages"] = [s for s in cfg["stages"] if s["key"] != "retention"]
    cfg["stages"][0]["name"] = "Release (renamed)"
    cfg["stages"].append({"key": "recirculation", "name": "Recirculation", "definition": "Synthetic fifth stage."})
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    stats = reload_config(project, OPERATOR)
    assert stats == {"added": 1, "updated": 3, "deactivated": 1} or (stats["added"], stats["deactivated"]) == (1, 1)
    assert [s["key"] for s in stages(conn)] == ["release", "transit", "discharge", "recirculation"]
    retired = conn.execute("SELECT * FROM stage WHERE key = 'retention'").fetchone()
    assert retired is not None and retired["active"] == 0
    # existing assignments to the deactivated stage are kept
    n = conn.execute("SELECT COUNT(*) FROM finding_stage WHERE stage_id = ?", (retired["id"],)).fetchone()[0]
    assert n > 0
    assert conn.execute("SELECT name FROM stage WHERE key = 'release'").fetchone()[0] == "Release (renamed)"
    actions = {e["action"] for e in audit.history(conn, entity_type="stage", limit=50)}
    assert {"stage.add", "stage.update", "stage.deactivate"} <= actions


# ------------------------------------------------------------------ reviewer identity and credentials
def test_reviewer_identity_comes_from_local_configuration(project, monkeypatch):
    with pytest.raises(ConfigError, match="No reviewer identity is configured"):
        resolve_reviewer(None, project.root)
    (project.root / "local.yaml").write_text("reviewer: Local File Reviewer\n", encoding="utf-8")
    assert resolve_reviewer(None, project.root) == "Local File Reviewer"
    monkeypatch.setenv("GEODESIC_REVIEWER", "Env Reviewer")
    assert resolve_reviewer(None, project.root) == "Env Reviewer"
    assert resolve_reviewer(" Explicit Reviewer ", project.root) == "Explicit Reviewer"
    assert load_local_settings(project.root).contact_email is None       # no contact address is built in


def test_credentials_are_read_from_env_or_untracked_file_and_redacted_from_logs(project, monkeypatch):
    secret = "synthetic-secret-token-12345"
    assert read_credential("GEODESIC_CROSSREF_PLUS_TOKEN", project.root) is None
    (project.root / "credentials.env").write_text(f"# local only\nGEODESIC_CROSSREF_PLUS_TOKEN='{secret}'\n",
                                                  encoding="utf-8")
    assert read_credential("GEODESIC_CROSSREF_PLUS_TOKEN", project.root) == secret
    monkeypatch.setenv("GEODESIC_CROSSREF_PLUS_TOKEN", secret + "-env")
    assert read_credential("GEODESIC_CROSSREF_PLUS_TOKEN", project.root) == secret + "-env"

    log = logging.getLogger("geodesic.test")
    log.info("calling provider with token %s", secret)
    log.info("request", extra={"url": f"https://api.example.invalid/works?mailto=someone@example.invalid&api_key={secret}",
                               "header": f"Authorization: Bearer {secret}"})
    try:
        raise RuntimeError(f"upstream rejected key {secret}")
    except RuntimeError:
        log.exception("request failed")
    for handler in logging.getLogger("geodesic").handlers:
        handler.flush()
    text = (project.logs_dir / "geodesic.log").read_text(encoding="utf-8")
    assert "calling provider with token" in text and "request failed" in text
    assert secret not in text and "someone@example.invalid" not in text
    assert REDACTED in text
    for line in text.splitlines():
        record = json.loads(line)                                        # structured: one JSON object per line
        assert {"ts", "level", "logger", "event"} <= set(record)
    # the credential is in neither the database nor the exportable configuration
    dump = "\n".join(project.conn.iterdump())
    assert secret not in dump


def test_redaction_helpers():
    assert redact("GET /x?api_key=abcd1234&rows=5") == f"GET /x?api_key={REDACTED}&rows=5"
    assert redact("mailto=person@example.invalid") == f"mailto={REDACTED}"
    assert "abc.def" not in redact("crossref-api-key: Bearer abc.def")
    assert redact_params({"query": "tracer", "email": "person@example.invalid", "apikey": "k"}) == {
        "query": "tracer", "email": REDACTED, "apikey": REDACTED}
    headers = redact_headers({"User-Agent": "geodesic (mailto:person@example.invalid)", "Accept": "application/json",
                              "Authorization": "Bearer t0ken"})
    assert headers == {"User-Agent": REDACTED, "Accept": "application/json", "Authorization": REDACTED}
