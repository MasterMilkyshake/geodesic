"""Normalisation and syntax checks for persistent identifiers.

Normalisation only makes two spellings of the same identifier comparable. It
does not establish that the identifier exists or that it belongs to the paper
citing it; that is *validation* (a metadata lookup) and *identity verification*
(a reviewer decision), which are recorded separately.
"""

from __future__ import annotations

import re
import urllib.parse

_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*|info:doi/)", re.IGNORECASE)
_DOI_SYNTAX_RE = re.compile(r"^10\.\d{4,9}/\S+$")
_DOI_FIND_RE = re.compile(r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)
_PMID_FIND_RE = re.compile(r"\bPMID:?\s*(\d{1,9})\b", re.IGNORECASE)
_PMCID_RE = re.compile(r"^PMC\d{1,9}$", re.IGNORECASE)
_TRAILING = ".,;:)]}>\"'"


def normalize_doi(raw: str | None) -> str | None:
    """Lower-cased DOI without resolver prefix or trailing punctuation; ``None`` if not DOI-shaped.

    DOIs are case-insensitive, so lower-casing is lossless for comparison. The
    raw string is always stored alongside the normalised value.
    """
    if raw is None:
        return None
    s = urllib.parse.unquote(str(raw).strip())
    s = _DOI_PREFIX_RE.sub("", s).strip()
    while s and s[-1] in _TRAILING:
        # keep a closing bracket that balances an opening one inside the suffix
        if s[-1] == ")" and s.count("(") >= s.count(")"):
            break
        s = s[:-1]
    s = s.lower()
    return s if _DOI_SYNTAX_RE.match(s) else None


def find_doi(text: str | None) -> tuple[str, str, int, int] | None:
    """First DOI in free text: ``(raw, normalized, start, end)`` of the raw DOI string."""
    if not text:
        return None
    for m in _DOI_FIND_RE.finditer(text):
        raw = m.group(1)
        norm = normalize_doi(raw)
        if norm:
            # trim the raw span to the characters that survived normalisation
            end = m.start(1) + len(norm)
            return text[m.start(1):end], norm, m.start(1), end
    return None


def normalize_pmid(raw: str | int | None) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    s = re.sub(r"^(?:PMID:?\s*)", "", s, flags=re.IGNORECASE)
    if s.endswith(".0"):
        s = s[:-2]
    return s.lstrip("0") or None if re.fullmatch(r"\d{1,9}", s) else None


def find_pmid(text: str | None) -> str | None:
    if not text:
        return None
    m = _PMID_FIND_RE.search(text)
    return normalize_pmid(m.group(1)) if m else None


def normalize_pmcid(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if re.fullmatch(r"\d{1,9}", s):
        s = "PMC" + s
    return s if _PMCID_RE.match(s) else None


def normalize_identifier(scheme: str, raw: str | None) -> str | None:
    """Normalise by scheme; unknown schemes are compared as stripped, case-folded text."""
    if raw is None:
        return None
    if scheme == "doi":
        return normalize_doi(raw)
    if scheme == "pmid":
        return normalize_pmid(raw)
    if scheme == "pmcid":
        return normalize_pmcid(raw)
    s = str(raw).strip().casefold()
    return s or None


def doi_from_url(url: str | None) -> str | None:
    """DOI carried by a doi.org URL (e.g. a workbook hyperlink), else ``None``."""
    if not url or "doi.org/" not in url.lower():
        return None
    return normalize_doi(url)
