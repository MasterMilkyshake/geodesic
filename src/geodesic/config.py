"""Configuration: project configuration, local (per-machine) settings and credentials.

Three layers, none of which contains machine-specific paths in source control:

``project.yaml`` (inside the project data directory)
    Stages, parser vocabulary, deduplication scores, search settings. Exportable.
``local.yaml`` (inside the project data directory, or ``~/.config/geodesic/config.yaml``)
    Reviewer identity and provider contact details for this machine. Never exported.
Environment variables / ``credentials.env``
    Secrets. Never stored in the database, logs, raw-response archives or exports.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import ConfigError
from .logging_utils import register_secret

ENV_HOME = "GEODESIC_HOME"
ENV_REVIEWER = "GEODESIC_REVIEWER"
ENV_CONTACT_EMAIL = "GEODESIC_CONTACT_EMAIL"
ENV_CROSSREF_PLUS_TOKEN = "GEODESIC_CROSSREF_PLUS_TOKEN"
CREDENTIAL_ENV_VARS = (ENV_CROSSREF_PLUS_TOKEN,)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StageConfig(_Model):
    """One project-defined top-level evidence category."""

    key: str
    name: str
    definition: str = ""
    keywords: list[str] = Field(default_factory=list)  # optional; used only for *suggested* stage assignments

    @field_validator("key")
    @classmethod
    def _key_ok(cls, v: str) -> str:
        if not v or not v.replace("_", "").replace("-", "").isalnum():
            raise ValueError("stage key must be alphanumeric with optional '_' or '-'")
        return v


class DedupConfig(_Model):
    """Documented, configurable bibliographic matching score (see docs/DEDUPLICATION.md)."""

    weight_title: float = 0.70
    weight_first_author: float = 0.15
    weight_year: float = 0.15
    candidate_threshold: float = 0.80       # pairs scoring at or above this become review candidates
    title_floor: float = 0.60               # pairs whose title similarity is below this are never candidates
    identifier_title_conflict_below: float = 0.35  # same identifier but titles this dissimilar -> conflict flag
    max_block_size: int = 200


class ParserConfig(_Model):
    """Vocabulary for the deterministic narrative parser. All lists are editable."""

    extra_qualifier_phrases: list[str] = Field(default_factory=list)
    extra_units: dict[str, str] = Field(default_factory=dict)   # unit symbol -> dimension name (no conversion)
    segment_on_semicolon: bool = True
    repeat_similarity_threshold: float = 0.92
    reimport_similarity_threshold: float = 0.60
    reimport_ambiguity_margin: float = 0.10


class ConceptGroup(_Model):
    """One protocol-level search concept: synonyms are OR-ed, groups are AND-ed."""

    label: str
    terms: list[str]


class SearchConfig(_Model):
    default_provider: str = "europepmc"
    page_size: int = 100
    max_records: int = 500
    min_interval_seconds: float = 0.5       # politeness delay between requests
    max_retries: int = 4
    backoff_seconds: float = 1.0
    timeout_seconds: float = 30.0
    tool_name: str = "geodesic"
    concepts: list[ConceptGroup] = Field(default_factory=list)
    provider_field: dict[str, str] = Field(default_factory=dict)     # provider -> field restriction (e.g. TITLE_ABS)
    provider_query_override: dict[str, str] = Field(default_factory=dict)  # provider -> exact source-specific query
    smoke_query: str = 'TITLE:"calibration"'  # small query for the bounded live smoke test
    smoke_max_records: int = 6
    smoke_page_size: int = 3


class ProjectConfig(_Model):
    """Contents of ``project.yaml``."""

    name: str
    stages: list[StageConfig]
    reviewers: list[str] = Field(default_factory=list)   # known reviewer names (for dashboard selection)
    parser: ParserConfig = Field(default_factory=ParserConfig)
    dedup: DedupConfig = Field(default_factory=DedupConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)

    @field_validator("stages")
    @classmethod
    def _stages_unique(cls, v: list[StageConfig]) -> list[StageConfig]:
        keys = [s.key for s in v]
        if len(keys) != len(set(keys)):
            raise ValueError("stage keys must be unique")
        return v


def load_yaml(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse YAML file {path}: {exc}") from exc


def dump_yaml(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True, width=100)


def load_project_config(path: Path) -> ProjectConfig:
    data = load_yaml(path)
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping with at least 'name' and 'stages'.")
    try:
        return ProjectConfig.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError
        raise ConfigError(f"Invalid project configuration in {path}:\n{exc}") from exc


def default_home() -> Path:
    """Directory that holds project data directories (``$GEODESIC_HOME`` or ``~/geodesic-data``)."""
    env = os.environ.get(ENV_HOME)
    return Path(env).expanduser() if env else Path.home() / "geodesic-data"


def user_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "geodesic" / "config.yaml"


class LocalSettings(_Model):
    """Per-machine settings. Stored outside source control and never exported."""

    reviewer: str | None = None
    contact_email: str | None = None


def load_local_settings(project_root: Path | None = None) -> LocalSettings:
    """Merge user-level config, project ``local.yaml`` and environment (highest priority)."""
    merged: dict[str, Any] = {}
    candidates = [user_config_path()]
    if project_root is not None:
        candidates.append(project_root / "local.yaml")
    for path in candidates:
        if path.is_file():
            data = load_yaml(path) or {}
            if not isinstance(data, dict):
                raise ConfigError(f"{path} must contain a YAML mapping.")
            merged.update({k: v for k, v in data.items() if k in LocalSettings.model_fields})
    if os.environ.get(ENV_REVIEWER):
        merged["reviewer"] = os.environ[ENV_REVIEWER]
    if os.environ.get(ENV_CONTACT_EMAIL):
        merged["contact_email"] = os.environ[ENV_CONTACT_EMAIL]
    return LocalSettings.model_validate(merged)


def resolve_reviewer(explicit: str | None, project_root: Path | None = None) -> str:
    """Reviewer identity: explicit argument, else ``GEODESIC_REVIEWER``, else local configuration."""
    if explicit and explicit.strip():
        return explicit.strip()
    name = load_local_settings(project_root).reviewer
    if name and name.strip():
        return name.strip()
    raise ConfigError(
        "No reviewer identity is configured. Pass --reviewer NAME, set the GEODESIC_REVIEWER environment "
        f"variable, or put 'reviewer: NAME' in {user_config_path()} or in the project's local.yaml."
    )


def read_credential(env_var: str, project_root: Path | None = None) -> str | None:
    """Read a secret from the environment or an untracked ``credentials.env`` file.

    The value is registered for log redaction and must never be persisted by callers.
    """
    value = os.environ.get(env_var)
    if not value:
        files = [user_config_path().parent / "credentials.env"]
        if project_root is not None:
            files.append(project_root / "credentials.env")
        for path in files:
            if path.is_file():
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        if k.strip() == env_var:
                            value = v.strip().strip("'\"")
    if value:
        register_secret(value)
    return value or None
