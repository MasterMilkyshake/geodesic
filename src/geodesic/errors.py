"""Exception types. Every message should say what happened and what to do next."""

from __future__ import annotations


class GeodesicError(Exception):
    """Base class for all application errors."""


class ConfigError(GeodesicError):
    """Invalid or missing configuration."""


class ProvenanceError(GeodesicError):
    """Declared synthetic/real classification is missing or conflicts with the project."""


class MappingError(GeodesicError):
    """The workbook mapping is invalid."""


class UnconfirmedMappingError(MappingError):
    """Required mapping entries are still marked unconfirmed."""


class ProtocolError(GeodesicError):
    """Invalid protocol operation."""


class ProvisionalProtocolError(ProtocolError):
    """An action that requires a confirmed protocol was attempted under a provisional one."""


class GroundingError(GeodesicError):
    """A source location or transcription does not resolve to the stored source text."""


class ReviewError(GeodesicError):
    """A review action is not permitted in the record's current state."""


class RetrievalError(GeodesicError):
    """An external retrieval failed after bounded retries."""


class NotFoundError(GeodesicError):
    """A referenced record does not exist."""


class ValidationError(GeodesicError):
    """Input failed schema or semantic validation."""
