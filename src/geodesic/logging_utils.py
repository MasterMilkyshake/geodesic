"""Structured (JSON-lines) logging with credential redaction.

Secrets are never written to logs: values registered with
:func:`register_secret` are masked wherever they appear, and common credential
patterns in URLs and headers are masked even when the value was not registered.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED]"

_SECRET_VALUES: set[str] = set()

# query-string / header style credentials and contact details
_PATTERNS = [
    re.compile(r"(?i)\b(api[_-]?key|apikey|access[_-]?token|token|secret|password|passwd|key)=([^&\s\"']+)"),
    re.compile(r"(?i)\b(mailto|email)=([^&\s\"']+)"),
    re.compile(r"(?i)\b(authorization|crossref-api-key|x-api-key|proxy-authorization)\b(\s*[:=]\s*)(bearer\s+)?([^\s,\"']+)"),
]

SENSITIVE_PARAM_NAMES = {"api_key", "apikey", "api-key", "key", "token", "access_token", "secret", "password",
                         "email", "mailto"}
SENSITIVE_HEADER_NAMES = {"authorization", "proxy-authorization", "crossref-api-key", "x-api-key", "cookie",
                          "set-cookie", "user-agent", "from"}


def register_secret(value: str | None) -> None:
    """Register a literal secret value so it is masked in every log record and redacted string.

    The URL-encoded forms of the value are registered too, because a secret sent
    as a request parameter appears percent-encoded in URLs and error messages.
    """
    if value and len(value) >= 4:
        _SECRET_VALUES.add(value)
        _SECRET_VALUES.add(urllib.parse.quote(value, safe=""))
        _SECRET_VALUES.add(urllib.parse.quote_plus(value))


def redact(text: str) -> str:
    """Mask registered secrets and credential-looking fragments in ``text``."""
    if not text:
        return text
    out = text
    for secret in sorted(_SECRET_VALUES, key=len, reverse=True):
        if secret in out:
            out = out.replace(secret, REDACTED)
    out = _PATTERNS[0].sub(lambda m: f"{m.group(1)}={REDACTED}", out)
    out = _PATTERNS[1].sub(lambda m: f"{m.group(1)}={REDACTED}", out)
    out = _PATTERNS[2].sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", out)
    return out


def redact_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """Copy of request parameters with credentials and contact details masked."""
    clean: dict[str, Any] = {}
    for k, v in (params or {}).items():
        if k.lower() in SENSITIVE_PARAM_NAMES:
            clean[k] = REDACTED
        else:
            clean[k] = redact(str(v)) if isinstance(v, str) else v
    return clean


def redact_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Copy of request headers with sensitive ones masked."""
    return {k: (REDACTED if k.lower() in SENSITIVE_HEADER_NAMES else redact(v)) for k, v in (headers or {}).items()}


_STD_ATTRS = set(logging.LogRecord("x", 0, "x", 0, "", (), None).__dict__.keys()) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts (UTC), level, logger, event, plus any ``extra`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        from .util import utc_now

        payload: dict[str, Any] = {
            "ts": utc_now(),
            "level": record.levelname,
            "logger": record.name,
            "event": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                payload[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return redact(json.dumps(payload, ensure_ascii=False, default=str))


def configure_logging(log_file: Path | None = None, level: int = logging.INFO, stderr: bool = False) -> logging.Logger:
    """Configure the ``geodesic`` logger. Safe to call repeatedly."""
    logger = logging.getLogger("geodesic")
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, "_geodesic", False):
            logger.removeHandler(handler)
            handler.close()
    handlers: list[logging.Handler] = []
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    if stderr:
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setFormatter(JsonFormatter())
        handler._geodesic = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    return logger
