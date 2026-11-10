"""Import search results exported from other tools: RIS and CSV.

The file is preserved byte-for-byte in the project (``search_files/<sha256>.<ext>``)
and recorded as a search run with ``mode='file_import'``. Identifiers read from a
file are *unvalidated*: a file can be edited by hand, so a matching DOI makes a
duplicate candidate for review, not an automatic grouping.

RIS (``.ris``), as read by this importer
----------------------------------------
* UTF-8 text (a byte-order mark is accepted). A field line is a two-character tag
  (letter, then letter or digit), two spaces, a hyphen, an optional space and the
  value: ``TI  - Title``. A line that is not a field line continues the previous
  field (joined with one space). ``TY`` starts a record, ``ER`` ends it; a final
  record without ``ER`` is still read and reported.
* Tags used: ``TY`` type (kept as the original code, e.g. ``JOUR``); ``TI``/``T1``
  title; ``AU``/``A1`` authors (repeatable, kept in order); ``PY``/``Y1`` year
  (first four digits); ``JO``/``JF``/``T2``/``JA`` journal or container (first
  present, in that order); ``DO`` DOI; ``AB``/``N2`` abstract; ``UR`` URL
  (repeatable); ``LA`` language; ``AN``/``ID`` accession values, where
  ``PMID: 123`` is read as a PMID and ``PMC123`` as a PMCID. Every other tag is
  kept with the stored record but not interpreted.

CSV (``.csv``), as read by this importer
----------------------------------------
* UTF-8 (BOM accepted), comma-separated, RFC 4180 quoting, one header row. Lines
  starting with ``#`` before the header are skipped.
* Column names are matched case-insensitively: ``title``; ``authors`` (separated
  by ``;``); ``year``; ``journal``; ``doi``; ``pmid``; ``pmcid``; ``abstract``;
  ``url``; ``publication type`` (or ``publication_type``/``type``); ``language``;
  ``record id`` (or ``record_id``/``id``). Other columns are kept with the stored
  record but not interpreted. A row with no title, DOI, PMID or PMCID is skipped
  and counted.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

from .. import provenance
from ..db import transaction
from ..errors import GeodesicError
from ..identifiers import find_pmid, normalize_pmcid
from ..project import Project, preserve_source_file
from ..util import Actor
from . import runs
from .runs import ParsedRecord

FORMATS = ("ris", "csv")
_RIS_LINE = re.compile(r"^([A-Z][A-Z0-9])  -(?: (.*))?$")
_YEAR = re.compile(r"(?<!\d)(1[5-9]\d\d|20\d\d)(?!\d)")


def _year(value: str | None) -> int | None:
    m = _YEAR.search(value or "")
    return int(m.group(1)) if m else None


# ------------------------------------------------------------------ RIS
def parse_ris(text: str) -> tuple[list[tuple[dict, ParsedRecord]], list[str]]:
    """Parse RIS text. Returns ``(records, notes)``; nothing is guessed for unreadable parts."""
    notes: list[str] = []
    out: list[tuple[dict, ParsedRecord]] = []
    current: list[list[str]] | None = None
    stray = 0

    def close(ended: bool) -> None:
        nonlocal current
        if current is None:
            return
        fields: dict[str, list[str]] = {}
        for tag, value in current:
            fields.setdefault(tag, []).append(value.strip())
        if not ended:
            notes.append(f"record {len(out) + 1} has no ER tag; it was read up to the next record or end of file")
        out.append((fields, _ris_record(fields)))
        current = None

    for line in text.lstrip("﻿").splitlines():
        m = _RIS_LINE.match(line.rstrip())
        if m:
            tag, value = m.group(1), m.group(2) or ""
            if tag == "TY":
                close(False)
                current = [[tag, value]]
            elif tag == "ER":
                close(True)
            elif current is not None:
                current.append([tag, value])
            else:
                stray += 1
        elif line.strip():
            if current:
                current[-1][1] = (current[-1][1] + " " + line.strip()).strip()
            else:
                stray += 1
    close(False)
    if stray:
        notes.append(f"{stray} line(s) outside any record were ignored")
    return out, notes


def _first(fields: dict[str, list[str]], *tags: str) -> str | None:
    for tag in tags:
        for v in fields.get(tag, []):
            if v:
                return v
    return None


def _ris_record(fields: dict[str, list[str]]) -> ParsedRecord:
    pmid = pmcid = None
    for v in fields.get("AN", []) + fields.get("ID", []):
        found = find_pmid(v)
        if found and not pmid:
            pmid = found[0] if isinstance(found, tuple) else found
        m = re.search(r"\bPMC\d{1,9}\b", v, re.IGNORECASE)
        if m and not pmcid:
            pmcid = normalize_pmcid(m.group(0))
    return ParsedRecord(
        title=_first(fields, "TI", "T1"), authors=[a for a in fields.get("AU", []) + fields.get("A1", []) if a],
        year=_year(_first(fields, "PY", "Y1")), journal=_first(fields, "JO", "JF", "T2", "JA"),
        publication_type=_first(fields, "TY"), language=_first(fields, "LA"), abstract=_first(fields, "AB", "N2"),
        doi=_first(fields, "DO"), pmid=str(pmid) if pmid else None, pmcid=pmcid,
        urls=[u for u in fields.get("UR", []) if u])


# ------------------------------------------------------------------ CSV
_CSV_COLUMNS = {"title": "title", "authors": "authors", "year": "year", "journal": "journal", "doi": "doi",
                "pmid": "pmid", "pmcid": "pmcid", "abstract": "abstract", "url": "url",
                "publication type": "publication_type", "publication_type": "publication_type",
                "type": "publication_type", "language": "language", "record id": "record_id",
                "record_id": "record_id", "id": "record_id"}


def parse_csv(text: str) -> tuple[list[tuple[dict, ParsedRecord]], list[str]]:
    """Parse CSV text. Returns ``(records, notes)``."""
    lines = text.lstrip("﻿").splitlines(keepends=True)
    while lines and (lines[0].startswith("#") or not lines[0].strip()):
        lines.pop(0)
    reader = csv.reader(io.StringIO("".join(lines)))
    try:
        header = next(reader)
    except StopIteration:
        raise GeodesicError("The CSV file has no header row.") from None
    roles = [_CSV_COLUMNS.get(h.strip().casefold()) for h in header]
    if not any(r in ("title", "doi", "pmid", "pmcid") for r in roles):
        raise GeodesicError("The CSV header needs at least one of the columns: title, doi, pmid, pmcid."
                            f" Found: {', '.join(h.strip() for h in header)}")
    notes: list[str] = []
    ignored = [h.strip() for h, r in zip(header, roles) if r is None and h.strip()]
    if ignored:
        notes.append("columns kept with the stored record but not interpreted: " + ", ".join(ignored))
    out: list[tuple[dict, ParsedRecord]] = []
    skipped = 0
    for row in reader:
        if not any(c.strip() for c in row):
            continue
        raw = {header[i].strip(): row[i] for i in range(min(len(header), len(row)))}
        got: dict[str, str] = {}
        for role, cell in zip(roles, row):
            if role and cell.strip() and role not in got:
                got[role] = cell.strip()
        if not any(got.get(k) for k in ("title", "doi", "pmid", "pmcid")):
            skipped += 1
            continue
        out.append((raw, ParsedRecord(
            provider_record_id=got.get("record_id"), title=got.get("title"),
            authors=[a.strip() for a in got.get("authors", "").split(";") if a.strip()],
            year=_year(got.get("year")), journal=got.get("journal"), publication_type=got.get("publication_type"),
            language=got.get("language"), abstract=got.get("abstract"), doi=got.get("doi"), pmid=got.get("pmid"),
            pmcid=got.get("pmcid"), urls=[got["url"]] if got.get("url") else [])))
    if skipped:
        notes.append(f"{skipped} row(s) without title, DOI, PMID or PMCID were skipped")
    return out, notes


PARSERS = {"ris": parse_ris, "csv": parse_csv}


def parse_file(path: Path, fmt: str | None = None) -> tuple[str, list[tuple[dict, ParsedRecord]], list[str]]:
    fmt = (fmt or Path(path).suffix.lstrip(".")).lower()
    if fmt not in PARSERS:
        raise GeodesicError(f"Unsupported search-result file format '{fmt}'. Supported: {', '.join(FORMATS)}.")
    try:
        text = Path(path).read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GeodesicError(f"{Path(path).name} is not UTF-8 text ({exc}). Re-export it as UTF-8.") from exc
    records, notes = PARSERS[fmt](text)
    return fmt, records, notes


# ------------------------------------------------------------------ import
def import_search_file(project: Project, path: Path | str, actor: Actor, *, fmt: str | None = None,
                       declare: str | None = None, source_label: str | None = None) -> dict[str, Any]:
    """Import an RIS or CSV file of search results as a ``file_import`` search run.

    ``source_label`` names where the file came from (database or tool), as stated
    by the person importing it. Importing the identical file again changes nothing.
    """
    path = Path(path)
    if not path.is_file():
        raise GeodesicError(f"Search-result file not found: {path}")
    tag = provenance.detect_embedded_tag(path)
    data_class = provenance.resolve_import_class(project.data_class, declare, tag, f"Search-result file {path.name}")
    fmt, records, notes = parse_file(path, fmt)
    stored, sha, _size = preserve_source_file(path, project.search_files_dir)
    conn = project.conn
    prior = conn.execute("SELECT id FROM search_run WHERE mode = 'file_import' AND source_file_sha256 = ?"
                         " AND status != 'failed'", (sha,)).fetchone()
    if prior:
        return {"status": "already_imported", "run": runs.get_run(conn, prior["id"]), "dedup": {}, "links": {},
                "notes": notes}
    if not records:
        raise GeodesicError(f"No records could be read from {path.name}. " + " ".join(notes))
    provider = f"file:{fmt}"
    run_id = runs.start_run(
        project, provider=provider, interface=f"{fmt.upper()} file import", mode="file_import",
        executed_query=f"(not recorded in the file) imported from {path.name}", actor=actor,
        declared_data_class=data_class, filters={"source_label": source_label, "format": fmt},
        source_file_name=path.name, source_file_sha256=sha)
    with transaction(conn):
        raw_id = runs.store_raw_response(project, run_id, 0, stored.read_bytes(), request_url=None,
                                         request_params={"file": path.name}, http_status=None,
                                         is_fixture=data_class == "synthetic", stored_path=stored)
        st = runs.ingest_records(project, run_id, raw_id, records, provider=provider, start_position=0,
                                 id_source="file_import", ids_validated=False, origin="file_import")
        conn.execute("UPDATE search_run SET pages_retrieved = 1, retrieved_count = ? WHERE id = ?",
                     (st["records"], run_id))
    links = {k: st[k] for k in ("new_paper", "exact_duplicate_record", "validated_identifier_match")}
    note = "; ".join(["the file states no provider total; completeness relative to the original search is unknown",
                      *notes])
    out = runs.finish_run(project, run_id, actor, status="complete", note=note,
                          new_paper_ids=st["new_paper_ids"], link_counts=links)
    out.update(status="imported", links=links, notes=notes)
    return out
