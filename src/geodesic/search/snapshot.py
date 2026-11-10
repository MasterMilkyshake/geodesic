"""Re-reading a stored search run (reproducibility by snapshot).

A search run is reproduced by re-reading the raw responses stored with it, not
by asking the provider again: provider indexes change, so the same query run
later can return different records, and that would be a *new* run.

:func:`verify_snapshot` checks that every stored raw response still has the
SHA-256 recorded at retrieval, parses the stored bytes again and checks that the
records, in order, are exactly the ones recorded for the run.
"""

from __future__ import annotations

from typing import Any

from ..errors import GeodesicError
from ..project import Project
from ..util import sha256_file
from . import europepmc, fileimport, runs
from .runs import ParsedRecord


def _parse(provider: str, content: bytes) -> list[tuple[Any, ParsedRecord]]:
    if provider == europepmc.PROVIDER:
        return europepmc.parse_page(content).items
    if provider.startswith("file:"):
        return fileimport.PARSERS[provider.split(":", 1)[1]](content.decode("utf-8"))[0]
    raise GeodesicError(f"No stored-response parser is registered for provider '{provider}'.")


def read_snapshot(project: Project, run_id: str) -> dict[str, Any]:
    """Records of a run re-read from its stored raw responses, with integrity checks."""
    conn = project.conn
    run = runs.get_run(conn, run_id)
    problems: list[str] = []
    records: list[dict[str, Any]] = []
    responses = conn.execute("SELECT * FROM search_raw_response WHERE search_run_id = ? ORDER BY seq",
                             (run_id,)).fetchall()
    for resp in responses:
        path = project.abs(resp["stored_path"])
        if not path.is_file():
            problems.append(f"raw response {resp['seq']} is missing from the project directory ({resp['stored_path']})")
            continue
        if sha256_file(path) != resp["sha256"]:
            problems.append(f"raw response {resp['seq']} no longer matches its recorded SHA-256")
            continue
        try:
            items = _parse(run["provider"], path.read_bytes())
        except GeodesicError as exc:
            # a response that could not be parsed at retrieval time is expected to fail the same way now
            stored = conn.execute("SELECT COUNT(*) FROM search_result WHERE raw_response_id = ?",
                                  (resp["id"],)).fetchone()[0]
            if stored:
                problems.append(f"raw response {resp['seq']} cannot be parsed any more: {exc}")
            continue
        # a response can hold more records than were imported from it (retrieval limit, resumed runs)
        imported = {r[0] for r in conn.execute("SELECT record_sha256 FROM search_result WHERE raw_response_id = ?",
                                               (resp["id"],))}
        for raw, rec in items:
            sha = runs.record_hash(run["provider"], raw)
            if sha in imported:
                records.append({"record_sha256": sha, "parsed": rec.model_dump(mode="json")})
    stored_hashes = [r["record_sha256"] for r in conn.execute(
        "SELECT record_sha256 FROM search_result WHERE search_run_id = ? ORDER BY position", (run_id,))]
    reread_hashes = [r["record_sha256"] for r in records]
    if not problems and reread_hashes != stored_hashes:
        problems.append(f"re-read records differ from the recorded results ({len(reread_hashes)} re-read,"
                        f" {len(stored_hashes)} recorded)")
    return {"run_id": run_id, "provider": run["provider"], "mode": run["mode"],
            "executed_query": run["executed_query"], "raw_responses": len(responses), "records": records,
            "recorded_results": len(stored_hashes), "intact": not problems, "problems": problems}


def verify_snapshot(project: Project, run_id: str) -> dict[str, Any]:
    """Like :func:`read_snapshot` without the record payloads."""
    out = read_snapshot(project, run_id)
    out["records"] = len(out["records"])
    return out
