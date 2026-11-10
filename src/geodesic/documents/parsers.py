"""Deterministic text extraction from PDF and XML documents.

PDF  -> one text unit per page. The unit key is the 0-based page index as text
        ("0" is the first page). The text is what the PDF library extracts, with
        line endings normalised to ``\\n`` and NUL characters removed; nothing
        else is changed, re-ordered or "cleaned". No OCR is performed: a page
        without a text layer is recorded as ``no_text`` (never as an empty page
        of a readable document).
XML  -> one text unit per text-bearing element (paragraph, title, ...). The unit
        key is the element's structural path from the root with 1-based sibling
        positions and local tag names, e.g. ``body[1]/sec[2]/sec[1]/p[3]``. The
        text is the element's character content with inline markup flattened and
        runs of whitespace collapsed to one space. ``table-wrap`` elements are
        stored as tables with addressable cells. Reference lists and
        bibliographic front matter other than title and abstract are not stored
        as text units.

Parsing never executes or interprets document content. XML is read with a
hardened parser: entity declarations and external references are refused, and
such a file is recorded as a failed parse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..identifiers import normalize_doi, normalize_pmcid, normalize_pmid
from ..util import canonical_json, sha256_text

PDF_PARSER = "pypdf-page-text"
XML_PARSER = "geodesic-xml-elements"
XML_PARSER_VERSION = "1.0"
FORMATS = {".pdf": "pdf", ".xml": "xml", ".nxml": "xml"}

_DOI_IN_TEXT = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)")
# JATS vocabulary used for structure; other XML falls back to the generic rules below.
_BLOCK_TAGS = {"p", "title", "article-title", "subtitle", "alt-title", "term", "attrib", "verse-line"}
_FLOAT_TAGS = {"table-wrap", "fig", "fig-group", "table-wrap-group", "boxed-text", "supplementary-material"}
_SKIP_TAGS = {"ref-list", "journal-meta", "contrib-group", "aff", "author-notes", "pub-date", "history",
              "permissions", "article-id", "article-categories", "kwd-group", "funding-group", "counts",
              "custom-meta-group", "self-uri", "related-article", "label", "object-id", "table", "graphic",
              "inline-graphic", "media", "tex-math", "alternatives", "xref"}
_FRONT_CONTAINERS = {"front": {"article-meta"}, "front-stub": {"title-group", "abstract", "trans-abstract"},
                     "article-meta": {"title-group", "abstract", "trans-abstract"},
                     "title-group": {"article-title", "subtitle"}}


@dataclass
class ParsedUnit:
    unit_kind: str                     # 'page' | 'element'
    unit_key: str
    seq: int
    text: str
    extraction_status: str = "ok"      # 'ok' | 'no_text' | 'failed'
    label: str | None = None
    section_path: str | None = None
    has_images: bool = False


@dataclass
class ParsedTable:
    table_key: str
    seq: int
    label: str | None
    caption: str | None
    cells: list[tuple[int, int, str, bool, int, int]]   # row_idx, col_idx, text, is_header, rowspan, colspan
    n_rows: int = 0
    n_cols: int = 0


@dataclass
class ParsedDocument:
    format: str
    parser: str
    parser_version: str
    status: str                        # 'ok' | 'partial' | 'image_only' | 'failed'
    failure_reason: str | None = None
    units: list[ParsedUnit] = field(default_factory=list)
    tables: list[ParsedTable] = field(default_factory=list)
    identifiers: dict[str, list[str]] = field(default_factory=dict)   # scheme -> normalised values found inside
    license: str | None = None
    title: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def n_chars(self) -> int:
        return sum(len(u.text) for u in self.units)

    @property
    def text_sha256(self) -> str:
        """Hash over everything that locations can address (unit texts and table cells)."""
        return sha256_text(canonical_json({
            "units": [[u.unit_key, u.extraction_status, u.text] for u in self.units],
            "tables": [[t.table_key, [list(c[:3]) for c in t.cells]] for t in self.tables]}))


def detect_format(path: Path | str) -> str | None:
    return FORMATS.get(Path(path).suffix.lower())


def _clean(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


# ---------------------------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------------------------
def _page_has_images(page: Any) -> bool:
    try:
        resources = page.get("/Resources")
        xobjects = resources.get_object().get("/XObject") if resources is not None else None
        if xobjects is None:
            return False
        return any(x.get_object().get("/Subtype") == "/Image" for x in xobjects.get_object().values())
    except Exception:
        return False


def dois_in_text(text: str) -> list[str]:
    """Normalised DOIs written in a text, in order of appearance, without repeats."""
    out: list[str] = []
    for m in _DOI_IN_TEXT.finditer(text or ""):
        norm = normalize_doi(m.group(1))
        if norm and norm not in out:
            out.append(norm)
    return out


def parse_pdf(path: Path | str) -> ParsedDocument:
    import pypdf
    from pypdf import PdfReader

    doc = ParsedDocument(format="pdf", parser=PDF_PARSER, parser_version=pypdf.__version__, status="failed")
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception:
                unlocked = 0
            if not unlocked:
                doc.failure_reason = "The PDF is encrypted and cannot be read without a password."
                return doc
        n_pages = len(reader.pages)
    except Exception as exc:
        doc.failure_reason = f"The file could not be read as a PDF ({type(exc).__name__})."
        return doc
    if n_pages == 0:
        doc.failure_reason = "The PDF contains no pages."
        return doc
    try:
        meta = reader.metadata or {}
        doc.title = str(meta.get("/Title")).strip() or None if meta.get("/Title") else None
    except Exception:
        doc.title = None
    for i in range(n_pages):
        status, text, images = "ok", "", False
        try:
            page = reader.pages[i]
            images = _page_has_images(page)
            text = _clean(page.extract_text() or "")
            if not text.strip():
                status, text = "no_text", ""
        except Exception as exc:
            status, text = "failed", ""
            doc.notes.append(f"page index {i}: text extraction failed ({type(exc).__name__})")
        doc.units.append(ParsedUnit("page", str(i), i, text, status, f"page {i + 1}", None, images))
    with_text = sum(1 for u in doc.units if u.extraction_status == "ok")
    if with_text == 0:
        doc.status = "image_only"
        doc.failure_reason = ("No page has an extractable text layer (for example a scanned document). "
                              "OCR is not implemented; the document is stored but has no usable text.")
    elif with_text < n_pages:
        doc.status = "partial"
        doc.failure_reason = f"{n_pages - with_text} of {n_pages} page(s) have no extractable text."
    else:
        doc.status = "ok"
    first = doc.units[0].text if doc.units[0].extraction_status == "ok" else ""
    found = dois_in_text(first)
    if found:
        doc.identifiers["doi"] = found
    return doc


# ---------------------------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------------------------
def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text_of(elem: Any, skip_floats: bool = True) -> str:
    """Character content with inline markup flattened; nested floats (tables, figures) are left out."""
    parts: list[str] = []

    def walk(e: Any, top: bool) -> None:
        if not isinstance(e.tag, str):                       # comment / processing instruction
            return
        if not top and skip_floats and _local(e.tag) in _FLOAT_TAGS:
            return
        if e.text:
            parts.append(e.text)
        for child in e:
            walk(child, False)
            if child.tail:
                parts.append(child.tail)

    walk(elem, True)
    return " ".join("".join(parts).split())


def _has_direct_text(elem: Any) -> bool:
    if elem.text and elem.text.strip():
        return True
    return any(child.tail and child.tail.strip() for child in elem)


def _children(elem: Any) -> Iterator[tuple[Any, str, str]]:
    """``(child, local_tag, key_step)`` with 1-based positions among same-named siblings."""
    counts: dict[str, int] = {}
    for child in elem:
        if not isinstance(child.tag, str):
            continue
        tag = _local(child.tag)
        counts[tag] = counts.get(tag, 0) + 1
        yield child, tag, f"{tag}[{counts[tag]}]"


def _table(elem: Any, key: str, seq: int) -> ParsedTable:
    label = caption = None
    cells: list[tuple[int, int, str, bool, int, int]] = []
    occupied: set[tuple[int, int]] = set()
    row_idx = -1

    def span(cell: Any, name: str) -> int:
        try:
            return max(1, int(cell.get(name) or 1))
        except ValueError:
            return 1

    for child in elem:
        tag = _local(child.tag)
        if tag == "label":
            label = _text_of(child) or None
        elif tag == "caption":
            caption = _text_of(child) or None
    for tr in elem.iter():
        if _local(tr.tag) != "tr":
            continue
        row_idx += 1
        col = 0
        for cell in tr:
            tag = _local(cell.tag)
            if tag not in ("td", "th"):
                continue
            while (row_idx, col) in occupied:
                col += 1
            rs, cs = span(cell, "rowspan"), span(cell, "colspan")
            for r in range(row_idx, row_idx + rs):
                for c in range(col, col + cs):
                    occupied.add((r, c))
            cells.append((row_idx, col, _text_of(cell, skip_floats=False), tag == "th", rs, cs))
            col += cs
    header_rows = set()
    for thead in elem.iter():
        if _local(thead.tag) == "thead":
            n_before = 0
            for tr in elem.iter():
                if _local(tr.tag) == "tr":
                    if any(tr is t for t in thead.iter()):
                        header_rows.add(n_before)
                    n_before += 1
    if header_rows:
        cells = [(r, c, t, h or r in header_rows, rs, cs) for r, c, t, h, rs, cs in cells]
    n_rows = max((r for r, _ in occupied), default=-1) + 1
    n_cols = max((c for _, c in occupied), default=-1) + 1
    return ParsedTable(key, seq, label, caption, cells, n_rows, n_cols)


def _xml_identifiers(root: Any) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    norm = {"doi": normalize_doi, "pmid": normalize_pmid, "pmcid": normalize_pmcid, "pmc": normalize_pmcid}
    for front in root:
        if _local(front.tag) not in ("front", "front-stub"):
            continue
        for e in front.iter():
            if _local(e.tag) != "article-id":
                continue
            kind = (e.get("pub-id-type") or "").lower()
            if kind not in norm:
                continue
            raw = (e.text or "").strip()
            if kind in ("pmc", "pmcid") and raw.isdigit():
                raw = "PMC" + raw
            value = norm[kind](raw)
            scheme = "pmcid" if kind == "pmc" else kind
            if value and value not in found.setdefault(scheme, []):
                found[scheme].append(value)
        break
    return {k: v for k, v in found.items() if v}


def _xml_license(root: Any) -> str | None:
    for e in root.iter():
        tag = _local(e.tag)
        if tag == "license":
            for name, value in e.attrib.items():
                if _local(name) == "href" and value.strip():
                    return value.strip()
            for sub in e.iter():
                if _local(sub.tag) == "license_ref" and (sub.text or "").strip():
                    return sub.text.strip()
            for sub in e.iter():              # licence URL given as a link inside the licence paragraph
                if sub is not e and _local(sub.tag) in ("ext-link", "uri"):
                    for name, value in sub.attrib.items():
                        if _local(name) == "href" and value.strip():
                            return value.strip()
            if (e.get("license-type") or "").strip():
                return e.get("license-type").strip()
        if tag in ("body", "back"):
            break
    return None


def parse_xml(path: Path | str) -> ParsedDocument:
    from defusedxml import ElementTree as SafeET
    from defusedxml.common import DefusedXmlException

    doc = ParsedDocument(format="xml", parser=XML_PARSER, parser_version=XML_PARSER_VERSION, status="failed")
    try:
        data = Path(path).read_bytes()
        root = SafeET.fromstring(data)
    except DefusedXmlException as exc:
        doc.failure_reason = ("The XML declares entities or external references, which the hardened parser refuses "
                              f"({type(exc).__name__}).")
        return doc
    except Exception as exc:
        doc.failure_reason = f"The file could not be parsed as XML ({type(exc).__name__}: {str(exc)[:120]})."
        return doc
    doc.identifiers = _xml_identifiers(root)
    doc.license = _xml_license(root)
    seq = 0

    def unit(elem: Any, key: str, tag: str, sections: list[str]) -> None:
        nonlocal seq
        text = _text_of(elem)
        if not text:
            return
        if tag == "article-title" and doc.title is None and key.startswith("front"):
            doc.title = text
        ident = elem.get("id")
        doc.units.append(ParsedUnit("element", key, seq, text, "ok", f"{tag}#{ident}" if ident else tag,
                                    " > ".join(sections) or None))
        seq += 1

    def walk(elem: Any, path: str, tag: str, sections: list[str]) -> None:
        nonlocal seq
        allowed = _FRONT_CONTAINERS.get(tag)
        if tag == "sec":
            title = next((c for c in elem if _local(c.tag) == "title"), None)
            sections = sections + [(_text_of(title) if title is not None else "") or "(untitled section)"]
        elif tag in ("abstract", "trans-abstract"):
            sections = sections + ["Abstract"]
        for child, ctag, step in _children(elem):
            key = f"{path}/{step}" if path else step
            if allowed is not None and ctag not in allowed:
                continue
            if ctag in _SKIP_TAGS:
                continue
            if ctag == "table-wrap":
                doc.tables.append(_table(child, key, len(doc.tables)))
                walk(child, key, ctag, sections)             # caption and footnote paragraphs
            elif ctag in _BLOCK_TAGS or _has_direct_text(child):
                unit(child, key, ctag, sections)
                for sub, stag, sstep in _nested_floats(child, key):
                    if stag == "table-wrap":
                        doc.tables.append(_table(sub, sstep, len(doc.tables)))
                    walk(sub, sstep, stag, sections)
            else:
                walk(child, key, ctag, sections)

    walk(root, "", _local(root.tag), [])
    if not doc.units and not doc.tables:
        doc.failure_reason = "The XML was parsed but contains no text-bearing elements."
        return doc
    doc.status = "ok"
    return doc


def _nested_floats(elem: Any, path: str) -> Iterator[tuple[Any, str, str]]:
    """Floats (tables, figures) nested inside a text block, with their structural keys."""
    for child, tag, step in _children(elem):
        key = f"{path}/{step}"
        if tag in _FLOAT_TAGS:
            yield child, tag, key
        else:
            yield from _nested_floats(child, key)


def parse_document(path: Path | str, fmt: str) -> ParsedDocument:
    if fmt == "pdf":
        return parse_pdf(path)
    if fmt == "xml":
        return parse_xml(path)
    raise ValueError(f"No parser for format {fmt!r}")
