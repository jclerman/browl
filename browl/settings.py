"""Operator settings (DESIGN.md §2.12): defaults in code, overridden by environment variables."""

import logging
import re
from pathlib import Path

from pydantic import AliasGenerator, Field, HttpUrl, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

UNTAGGED = "none"
"""Reserved `LANGUAGE_RANKING` entry for values without a language tag."""

_RESERVED_PREFIX = re.compile(r"_[A-Za-z0-9._~-]+")  # one path segment of URL-safe characters
_LANGUAGE_TAG = re.compile(r"[a-z]{1,8}(-[a-z0-9]{1,8})*")
_PREFIX_NAME = re.compile(r"([A-Za-z_][A-Za-z0-9._-]*)?")  # empty is the default prefix
_IRI_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_JAVA_HEAP = re.compile(r"[1-9][0-9]*[kKmMgG]")  # -Xmx syntax, with a unit: a bare number would be bytes


def confirm_unique(values: list[str]) -> list[str]:
    """Confirm that all values in a list are unique.

    Args:
        values: List of values to check.

    Returns:
        List of unique values, if they are all unique.

    Raises:
        ValueError: If any values are not unique; reports the unique set of values that were not unique,
            in the order in which their second occurrence appeared.
    """
    val_set = set(values)
    if len(val_set) == len(values):
        return values
    duplicates: dict[str, None] = {}  # a dict, not a set: each duplicate once, in order
    for v in values:
        if v in val_set:
            val_set.remove(v)
        else:
            duplicates[v] = None
    raise ValueError(f"duplicate entries: {', '.join(duplicates)}")


class Settings(BaseSettings):
    """Every operator setting.

    `Settings()` (via pydantic-settings' `BaseSettings`) reads each setting from the environment variable named after
    it in uppercase, e.g. `base_url` from `BASE_URL`; lowercase or mixed-case names are ignored. Unset settings get
    their defaults. Each value arrives as a string and is converted to the field's type: lists and mappings are parsed
    as JSON (e.g. `LANGUAGE_RANKING='["en", "fr", "none"]'`), booleans accept `true`/`false` and `1`/`0`. The
    validators below then run; validation errors name the environment variable. The environment is read on each
    instantiation, not at import.

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

    # Match environment variables by their uppercase names only, so `base_url` can't also be set as `Base_Url`.
    model_config = SettingsConfigDict(
        frozen=True, case_sensitive=True, alias_generator=AliasGenerator(validation_alias=str.upper)
    )

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
        """Require a Java heap size in `-Xmx` syntax with a unit, such as 512m or 8g."""
        if not _JAVA_HEAP.fullmatch(value):
            raise ValueError("must be a Java heap size, such as 512m or 8g")
        return value

    @field_validator("name_properties")
    @classmethod
    def check_name_properties(cls, value: list[str]) -> list[str]:
        """Reject duplicate name properties."""
        return confirm_unique(value)

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
        return confirm_unique(ranking)

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
        return confirm_unique([address.lower() for address in value])

    @field_validator("auth_disabled")
    @classmethod
    def check_auth(cls, value: bool, info: ValidationInfo) -> bool:
        """Require the OIDC settings and seed admins unless authentication is disabled (O-6, O-7)."""
        # A field validator, not a model validator: its error shows only this field's value, never the client secret.
        # The fields it checks are declared earlier, so they're validated first; one that failed is absent from
        # info.data and has its own error already.
        if not value:
            required = ("oidc_client_id", "oidc_client_secret", "seed_admins")
            missing = [field.upper() for field in required if field in info.data and not info.data[field]]
            if missing:
                raise ValueError(f"{', '.join(missing)} required unless AUTH_DISABLED is true")
        return value


def load_settings() -> Settings:
    """Read and validate the settings at startup, warning if authentication is off.

    Raises:
        pydantic.ValidationError: A setting is missing or invalid.
    """
    settings = Settings()  # type: ignore[call-arg]  # required fields come from the environment
    if settings.auth_disabled:
        logger.warning("AUTH_DISABLED is true: authentication is OFF. Never use this setting in production.")
    return settings
