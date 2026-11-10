"""Declared synthetic/real provenance.

The application enforces *declared* provenance only. It cannot and does not
detect undisclosed synthetic content: a file is synthetic when it carries the
embedded GEODESIC synthetic tag or when the person importing it says so.

Rules
-----
* A project database is created as ``synthetic`` or ``real``; the flag is immutable.
* Bundled synthetic fixtures carry an embedded tag (:data:`TAG_TEXT`).
* An untagged import must be declared (``--declare synthetic|real``).
* A tagged-synthetic file can never be declared real.
* The effective class of an import must equal the project's class.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from .errors import ProvenanceError

DATA_CLASSES = ("synthetic", "real")
TAG_PROPERTY = "geodesic_data_class"
TAG_TEXT = "geodesic-data-class: synthetic"
SYNTHETIC_LABEL = "SYNTHETIC DATA: generated test fixture, not research evidence"

_tag_re = re.compile(r"geodesic[-_]data[-_]class\s*[:=]\s*synthetic", re.IGNORECASE)


def detect_embedded_tag(path: Path | str) -> str | None:
    """Return ``'synthetic'`` when the file carries the GEODESIC synthetic tag, else ``None``.

    Supported carriers: XLSX custom document property ``geodesic_data_class``;
    PDF document information (Keywords/Subject); a marker string within the first
    4 kB of XML, HTML, RIS, BibTeX, JSON or plain-text files. The absence of a tag
    says nothing about whether content is real.
    """
    p = Path(path)
    suffix = p.suffix.lower()
    try:
        if suffix in (".xlsx", ".xlsm"):
            with zipfile.ZipFile(p) as zf:
                if "docProps/custom.xml" in zf.namelist():
                    xml = zf.read("docProps/custom.xml").decode("utf-8", "replace")
                    m = re.search(
                        r'name="' + TAG_PROPERTY + r'"[^>]*>\s*<[^>]*lpwstr[^>]*>([^<]*)<', xml, re.IGNORECASE
                    )
                    if m and m.group(1).strip().lower() == "synthetic":
                        return "synthetic"
            return None
        if suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(p))
            meta = reader.metadata or {}
            blob = " ".join(str(meta.get(k, "")) for k in ("/Keywords", "/Subject", "/Title"))
            return "synthetic" if _tag_re.search(blob) else None
        with open(p, "rb") as fh:
            head = fh.read(4096).decode("utf-8", "replace")
        return "synthetic" if _tag_re.search(head) else None
    except Exception:
        # An unreadable file simply has no detectable tag; the declaration rules still apply.
        return None


def resolve_import_class(project_class: str, declared: str | None, embedded_tag: str | None, what: str) -> str:
    """Decide the effective data class of an import or raise :class:`ProvenanceError`."""
    if declared is not None and declared not in DATA_CLASSES:
        raise ProvenanceError(f"Declared data class must be one of {DATA_CLASSES}, got {declared!r}.")
    if embedded_tag == "synthetic":
        if declared == "real":
            raise ProvenanceError(
                f"{what} carries the embedded GEODESIC synthetic tag but was declared 'real'. "
                "Synthetic fixtures cannot be imported as real evidence."
            )
        effective = "synthetic"
    elif declared is None:
        raise ProvenanceError(
            f"{what} carries no provenance tag. Declare its provenance explicitly with "
            "--declare synthetic or --declare real."
        )
    else:
        effective = declared
    if effective != project_class:
        raise ProvenanceError(
            f"{what} is declared '{effective}' but this project is '{project_class}'. "
            "Synthetic and real evidence are kept in separate projects; import it into a project "
            f"created with --data-class {effective}."
        )
    return effective


def require_live_allowed(project_class: str, what: str) -> None:
    """Live provider responses describe real publications and are refused in synthetic projects."""
    if project_class != "real":
        raise ProvenanceError(
            f"{what} retrieves real bibliographic records and is not permitted in a synthetic project. "
            "Use fixture responses in synthetic projects, or run it in a project created with --data-class real."
        )
