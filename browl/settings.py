"""Operator settings (DESIGN.md §2.12): defaults in code, overridden by environment variables.

Each setting is read from the environment variable of the same name, uppercased. Lists and mappings are given as JSON,
e.g. `LANGUAGE_RANKING='["en", "fr", "none"]'`.
"""

import logging
import re
from pathlib import Path
from typing import Self

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

UNTAGGED = "none"
"""Reserved `LANGUAGE_RANKING` entry for values without a language tag."""

_RESERVED_PREFIX = re.compile(r"_[A-Za-z0-9._~-]+")  # one path segment of URL-safe characters
_LANGUAGE_TAG = re.compile(r"[a-z]{1,8}(-[a-z0-9]{1,8})*")
_PREFIX_NAME = re.compile(r"([A-Za-z_][A-Za-z0-9._-]*)?")  # empty is the default prefix
_IRI_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_JAVA_HEAP = re.compile(r"[1-9][0-9]*[kKmMgG]?")  # -Xmx syntax


def _unique(values: list[str]) -> list[str]:
    duplicates = sorted({v for v in values if values.count(v) > 1})
    if duplicates:
        raise ValueError(f"duplicate entries: {', '.join(duplicates)}")
    return values


class Settings(BaseSettings):
    """Every operator setting.

    Attributes:
        base_url: Where the service is served; IRIs under it get their native paths (O-2). Normalized as a URL, so
            compare IRIs with it in URL form (DESIGN.md §2.3).
        reserved_prefix: First path segment of the service's own pages (O-3).
        name_properties: Annotation properties for display names and search, best first; CURIEs or IRIs (O-4).
        language_ranking: Language tags, best first, lowercased; `UNTAGGED` is appended when not listed (O-5).
        extra_prefixes: CURIE prefix → namespace, overriding the ontology's own and the built-in ones (O-11).
        page_cache_size: Rendered pages kept in memory; 0 turns the cache off (O-10).
        reasoner_timeout_s: Reasoner time limit in seconds; placeholder until spike S-2 (O-8).
        reasoner_max_heap: Reasoner heap limit; placeholder until spike S-2 (O-8).
        app_jvm_max_heap: Heap for the app's JVM, which only renders per-page fragments; to be confirmed in P-4.
        fuseki_url: Fuseki's address.
        robot_jar: Path to `robot.jar`; placeholder until P-3 builds the image.
        jena_home: Jena's command-line tools; placeholder until P-3 builds the image.
        data_dir: Mounted volume for data that survives restarts (O-9).
        oidc_client_id: Google OAuth client ID (O-6).
        oidc_client_secret: Google OAuth client secret (O-6).
        seed_admins: Admin email addresses, lowercased; they can't be removed in the UI (O-6).
        auth_disabled: Turns authentication off, for development only (O-7).
    """

    model_config = SettingsConfigDict(frozen=True)

    base_url: HttpUrl
    reserved_prefix: str = "_browl"
    name_properties: list[str] = Field(default=["rdfs:label"], min_length=1)
    language_ranking: list[str] = ["en"]
    extra_prefixes: dict[str, str] = {}
    page_cache_size: int = Field(default=100, ge=0)
    reasoner_timeout_s: int = Field(default=3600, gt=0)
    reasoner_max_heap: str = "8g"
    app_jvm_max_heap: str = "1g"
    fuseki_url: HttpUrl = HttpUrl("http://fuseki:3030")
    robot_jar: Path = Path("/opt/robot/robot.jar")
    jena_home: Path = Path("/opt/jena")
    data_dir: Path = Path("/data")
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    seed_admins: list[str] = []
    auth_disabled: bool = False

    @field_validator("base_url", "fuseki_url")
    @classmethod
    def check_service_url(cls, value: HttpUrl) -> HttpUrl:
        """Reject a URL with a query string or fragment."""
        if value.query or value.fragment:
            raise ValueError("must not have a query string or fragment")
        return value

    @field_validator("reasoner_max_heap", "app_jvm_max_heap")
    @classmethod
    def check_java_heap(cls, value: str) -> str:
        """Require a Java heap size in `-Xmx` syntax, such as 512m or 8g."""
        if not _JAVA_HEAP.fullmatch(value):
            raise ValueError("must be a Java heap size, such as 512m or 8g")
        return value

    @field_validator("name_properties")
    @classmethod
    def check_name_properties(cls, value: list[str]) -> list[str]:
        """Reject duplicate name properties."""
        return _unique(value)

    @field_validator("reserved_prefix")
    @classmethod
    def check_reserved_prefix(cls, value: str) -> str:
        """Require one URL-safe path segment that starts with '_' (O-3)."""
        if not _RESERVED_PREFIX.fullmatch(value):
            raise ValueError("must start with '_' and be one path segment of letters, digits and '._~-', e.g. _browl")
        return value

    @field_validator("language_ranking")
    @classmethod
    def check_language_ranking(cls, value: list[str]) -> list[str]:
        """Lowercase and check the language tags, appending `UNTAGGED` when it isn't listed (O-5)."""
        ranking = [tag.lower() for tag in value]
        invalid = [tag for tag in ranking if tag != UNTAGGED and not _LANGUAGE_TAG.fullmatch(tag)]
        if invalid:
            raise ValueError(f"invalid language tags: {', '.join(invalid)}")
        if UNTAGGED not in ranking:
            ranking.append(UNTAGGED)
        return _unique(ranking)

    @field_validator("extra_prefixes")
    @classmethod
    def check_extra_prefixes(cls, value: dict[str, str]) -> dict[str, str]:
        """Require valid prefix names and absolute namespace IRIs (O-11)."""
        for prefix, namespace in value.items():
            if not _PREFIX_NAME.fullmatch(prefix):
                raise ValueError(f"invalid prefix name {prefix!r}")
            if not _IRI_SCHEME.match(namespace):
                raise ValueError(f"namespace for {prefix!r} must be an absolute IRI")
        return value

    @field_validator("seed_admins")
    @classmethod
    def check_seed_admins(cls, value: list[str]) -> list[str]:
        """Check and lowercase the seed admin email addresses, rejecting duplicates."""
        invalid = [address for address in value if not re.fullmatch(r"[^@\s]+@[^@\s]+", address)]
        if invalid:
            raise ValueError(f"not email addresses: {', '.join(invalid)}")
        return _unique([address.lower() for address in value])

    @model_validator(mode="after")
    def check_auth(self) -> Self:
        """Require the OIDC settings and seed admins unless authentication is disabled (O-6, O-7)."""
        if not self.auth_disabled:
            required = {
                "OIDC_CLIENT_ID": self.oidc_client_id,
                "OIDC_CLIENT_SECRET": self.oidc_client_secret,
                "SEED_ADMINS": self.seed_admins,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError(f"{', '.join(missing)} required unless AUTH_DISABLED is true")
        return self


def load_settings() -> Settings:
    """Read and validate the settings at startup, warning if authentication is off.

    Raises:
        pydantic.ValidationError: A setting is missing or invalid.
    """
    settings = Settings()  # type: ignore[call-arg]  # required fields come from the environment
    if settings.auth_disabled:
        logger.warning("AUTH_DISABLED is true: authentication is OFF. Never use this setting in production.")
    return settings
