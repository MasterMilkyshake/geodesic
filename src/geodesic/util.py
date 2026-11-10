"""Small shared helpers: IDs, UTC timestamps, hashing, canonical JSON, actors."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ACTOR_KINDS = ("human", "automated", "simulated", "system")


@dataclass(frozen=True)
class Actor:
    """Who performed an action.

    ``kind`` is one of ``human`` (a named reviewer from local configuration),
    ``automated`` (a named rule-based component), ``simulated`` (scripted
    demonstration actions; never to be read as human verification) or ``system``
    (bookkeeping such as migrations).
    """

    name: str
    kind: str = "human"

    def __post_init__(self) -> None:
        if self.kind not in ACTOR_KINDS:
            raise ValueError(f"Unknown actor kind {self.kind!r}; expected one of {ACTOR_KINDS}")
        if not self.name or not self.name.strip():
            raise ValueError("Actor name must not be empty")


SYSTEM = Actor("geodesic", "system")


def utc_now() -> str:
    """Current time in UTC as ISO 8601 with an explicit ``+00:00`` offset."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def utc_today() -> str:
    """Current UTC date (``YYYY-MM-DD``)."""
    return datetime.now(timezone.utc).date().isoformat()


def new_id(prefix: str) -> str:
    """Stable internal identifier: a type prefix plus 80 random bits (hex)."""
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path | str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def canonical_json(obj: Any) -> str:
    """Deterministic JSON used for hashing and stored snapshots."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def loads(text: str | None, default: Any = None) -> Any:
    """``json.loads`` that tolerates ``None``/empty input."""
    if text is None or text == "":
        return default
    return json.loads(text)


_slug_re = re.compile(r"[^a-z0-9]+")


def slugify(text: str, max_len: int = 48) -> str:
    """ASCII, lower-case, underscore-separated key derived from free text."""
    norm = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    slug = _slug_re.sub("_", norm).strip("_")
    return slug[:max_len].strip("_") or "x"


_ws_re = re.compile(r"\s+")
_punct_re = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_text_key(text: str) -> str:
    """Case-, punctuation- and whitespace-insensitive form used for identity matching.

    This is a matching key only; the original text is always stored unchanged.
    """
    t = unicodedata.normalize("NFKC", text or "").casefold()
    t = _punct_re.sub(" ", t)
    return _ws_re.sub(" ", t).strip()


def chunked(seq: list, size: int):
    """Yield consecutive slices of ``seq`` with at most ``size`` items."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
