"""Tests for the settings module."""

import logging
import re
from pathlib import Path

import pytest
from pydantic import HttpUrl, ValidationError

from browl.settings import UNTAGGED, Settings, confirm_unique, load_settings

AUTH = {
    "OIDC_CLIENT_ID": "client-id",
    "OIDC_CLIENT_SECRET": "client-secret",
    "SEED_ADMINS": '["Admin@Example.org"]',
}

INVALID_TAG = "invalid language tags"
DUPLICATES = "duplicate entries"
HEAP_SIZE = "must be a Java heap size"
PREFIX_RULE = "must start with '_' and be one path segment"
NOT_EMAIL = "not email addresses"

DEFAULTS = [
    ("reserved_prefix", "_browl"),
    ("name_properties", ["rdfs:label"]),
    ("language_ranking", ["en", UNTAGGED]),
    ("extra_prefixes", {}),
    ("page_cache_size", 100),
    ("reasoner_timeout_s", 3600),
    ("reasoner_max_heap", "8g"),
    ("app_jvm_max_heap", "1g"),
    ("fuseki_url", HttpUrl("http://fuseki:3030")),
    ("robot_jar", Path("/opt/robot/robot.jar")),
    ("jena_home", Path("/opt/jena")),
    ("data_dir", Path("/data")),
    ("max_upload_bytes", 20 * 10**9),
    ("oidc_client_id", None),
    ("oidc_client_secret", None),
    ("seed_admins", []),
    ("auth_disabled", False),
]
"""Every setting with a default (all but BASE_URL), and the default."""


@pytest.mark.parametrize(
    ("values", "duplicates"),
    [
        ([], None),
        (["a"], None),
        (["a", "b", "c"], None),
        (["a", "A"], None),
        (["a", "a"], "a"),
        (["a", "b", "a", "a", "c", "b"], "a, b"),
        (["a", "b", "b", "a"], "b, a"),
    ],
)
def test_confirm_unique(values: list[str], duplicates: str | None) -> None:
    """Check that confirm_unique returns a list without duplicates and otherwise names each duplicate once."""
    if duplicates is None:
        assert confirm_unique(values) is values
    else:
        with pytest.raises(ValueError, match=f"^duplicate entries: {duplicates}$"):
            confirm_unique(values)


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Provide only the required settings, ignoring any the developer has set."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BASE_URL", "https://example.org/onto")
    for name, value in AUTH.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def settings_with(settings_env: pytest.MonkeyPatch, **values: str) -> Settings:
    """Load the settings after setting the given environment variables."""
    for name, value in values.items():
        settings_env.setenv(name, value)
    return load_settings()


def test_every_default_is_tested() -> None:
    """Check that DEFAULTS lists every setting except the required BASE_URL."""
    assert {name for name, _ in DEFAULTS} == set(Settings.model_fields) - {"base_url"}


@pytest.mark.parametrize(("attribute", "default"), DEFAULTS)
def test_default(settings_env: pytest.MonkeyPatch, attribute: str, default: object) -> None:
    """Check that an unset setting gets its documented default."""
    if attribute.upper() in AUTH:  # the fixture sets these; they're optional only with authentication off
        for name in AUTH:
            settings_env.delenv(name)
        settings_env.setenv("AUTH_DISABLED", "true")
    assert getattr(load_settings(), attribute) == default


def test_base_url_is_required(settings_env: pytest.MonkeyPatch) -> None:
    """Check that BASE_URL is required."""
    settings_env.delenv("BASE_URL")
    with pytest.raises(ValidationError, match="BASE_URL"):
        load_settings()


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("BASE_URL", "https://example.org", HttpUrl("https://example.org/")),
        ("BASE_URL", "HTTPS://Example.ORG:443/onto/", HttpUrl("https://example.org/onto/")),
        ("BASE_URL", "https://bücher.example/onto/Bär", HttpUrl("https://xn--bcher-kva.example/onto/B%C3%A4r")),
        ("RESERVED_PREFIX", "_x", "_x"),
        ("RESERVED_PREFIX", "_a.b~c-d_e", "_a.b~c-d_e"),
        ("LANGUAGE_RANKING", '["en", "fr"]', ["en", "fr", UNTAGGED]),
        ("LANGUAGE_RANKING", '["EN-gb"]', ["en-gb", UNTAGGED]),
        ("LANGUAGE_RANKING", '["zh-Hant-TW", "iw"]', ["zh-hant-tw", "iw", UNTAGGED]),
        ("LANGUAGE_RANKING", '["none", "en"]', [UNTAGGED, "en"]),
        ("LANGUAGE_RANKING", "[]", [UNTAGGED]),
        (
            "NAME_PROPERTIES",
            '["skos:prefLabel", "http://www.w3.org/2000/01/rdf-schema#label"]',
            ["skos:prefLabel", "http://www.w3.org/2000/01/rdf-schema#label"],
        ),
        ("EXTRA_PREFIXES", '{"ex": "http://example.org/", "": "urn:x:"}', {"ex": "http://example.org/", "": "urn:x:"}),
        ("SEED_ADMINS", '["Admin@Example.org"]', ["admin@example.org"]),
    ],
)
def test_valid_setting_is_stored(settings_env: pytest.MonkeyPatch, name: str, value: str, expected: object) -> None:
    """Check that a valid value is accepted and stored as `expected`: normalized, lowercased or parsed from JSON."""
    assert getattr(settings_with(settings_env, **{name: value}), name.lower()) == expected


@pytest.mark.parametrize("missing", list(AUTH))
def test_auth_settings_required(settings_env: pytest.MonkeyPatch, missing: str) -> None:
    """Check that each OIDC setting and SEED_ADMINS is required while authentication is on (O-6)."""
    settings_env.delenv(missing)
    with pytest.raises(ValidationError, match=f"{missing}.* required unless AUTH_DISABLED"):
        load_settings()


def test_errors_never_show_the_client_secret(settings_env: pytest.MonkeyPatch) -> None:
    """Check that the OIDC client secret doesn't appear in a validation error."""
    settings_env.delenv("OIDC_CLIENT_ID")
    with pytest.raises(ValidationError) as error:
        load_settings()
    assert "client-secret" not in str(error.value)


def test_load_settings_warns_when_auth_disabled(
    settings_env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Check that AUTH_DISABLED makes the auth settings optional and logs a warning (O-7)."""
    for name in AUTH:
        settings_env.delenv(name)
    with caplog.at_level(logging.WARNING):
        s = settings_with(settings_env, AUTH_DISABLED="true")
    assert s.auth_disabled is True
    assert "authentication is OFF" in caplog.text


def test_load_settings_no_warning_with_auth(settings_env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Check that nothing is logged when authentication is on."""
    with caplog.at_level(logging.WARNING):
        load_settings()
    assert caplog.text == ""


@pytest.mark.parametrize(
    ("variables", "expected"),
    [
        ({"page_cache_size": "7"}, 100),
        ({"Page_Cache_Size": "7"}, 100),
        ({"page_cache_size": "7", "PAGE_CACHE_SIZE": "8"}, 8),
    ],
)
def test_only_uppercase_env_names_are_read(
    settings_env: pytest.MonkeyPatch, variables: dict[str, str], expected: int
) -> None:
    """Check that only the all-uppercase variable is read; other casings are ignored."""
    assert settings_with(settings_env, **variables).page_cache_size == expected


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("BASE_URL", "example.org", "relative URL without a base"),
        ("BASE_URL", "ftp://example.org", "scheme should be 'http' or 'https'"),
        ("BASE_URL", "https://", "empty host"),
        ("BASE_URL", "https://ex ample.org", "invalid international domain name"),
        ("BASE_URL", "https://x.org/?a=1", "must not have a query string or fragment"),
        ("BASE_URL", "https://x.org/#a", "must not have a query string or fragment"),
        ("FUSEKI_URL", "fuseki:3030", "scheme should be 'http' or 'https'"),
        ("RESERVED_PREFIX", "browl", PREFIX_RULE),
        ("RESERVED_PREFIX", "_", PREFIX_RULE),
        ("RESERVED_PREFIX", "_a/b", PREFIX_RULE),
        ("RESERVED_PREFIX", "/_browl", PREFIX_RULE),
        ("RESERVED_PREFIX", "_a b", PREFIX_RULE),
        ("RESERVED_PREFIX", "_a%20", PREFIX_RULE),
        ("LANGUAGE_RANKING", '["en", "EN"]', f"{DUPLICATES}: en$"),
        ("LANGUAGE_RANKING", '["en_US"]', f"{INVALID_TAG}: en_us$"),
        ("LANGUAGE_RANKING", '["toolonglanguage"]', f"{INVALID_TAG}: toolonglanguage$"),
        ("LANGUAGE_RANKING", '["xx"]', f"{INVALID_TAG}: xx$"),
        ("LANGUAGE_RANKING", '["en-yy"]', f"{INVALID_TAG}: en-yy$"),
        ("LANGUAGE_RANKING", '["en-latm"]', f"{INVALID_TAG}: en-latm$"),
        ("LANGUAGE_RANKING", '["en-gb-foo"]', f"{INVALID_TAG}: en-gb-foo$"),
        ("LANGUAGE_RANKING", '"en"', "should be a valid list"),
        ("NAME_PROPERTIES", "[]", "at least 1 item"),
        ("NAME_PROPERTIES", '["rdfs:label", "rdfs:label"]', f"{DUPLICATES}: rdfs:label$"),
        ("EXTRA_PREFIXES", '{"1x": "http://example.org/"}', "invalid prefix name '1x'"),
        ("EXTRA_PREFIXES", '{"ex": "example.org/"}', "namespace for 'ex' must be an absolute IRI"),
        ("PAGE_CACHE_SIZE", "-1", "greater than or equal to 0"),
        ("MAX_UPLOAD_BYTES", "0", "greater than 0"),
        ("REASONER_TIMEOUT_S", "0", "greater than 0"),
        ("APP_JVM_MAX_HEAP", "1 g", HEAP_SIZE),
        ("APP_JVM_MAX_HEAP", "1024", HEAP_SIZE),
        ("REASONER_MAX_HEAP", "0g", HEAP_SIZE),
        ("SEED_ADMINS", '["admin"]', f"{NOT_EMAIL}: admin$"),
        ("SEED_ADMINS", '["a@b@c"]', f"{NOT_EMAIL}: a@b@c$"),
        ("SEED_ADMINS", '["a@b.org", "A@b.org"]', f"{DUPLICATES}: a@b.org$"),
    ],
)
def test_invalid_setting_is_rejected(settings_env: pytest.MonkeyPatch, name: str, value: str, message: str) -> None:
    """Check that an invalid value raises one ValidationError, naming the variable, whose message matches `message`."""
    with pytest.raises(ValidationError) as error:
        settings_with(settings_env, **{name: value})
    [reported] = error.value.errors()
    assert reported["loc"] == (name,)
    assert re.search(message, reported["msg"]), reported["msg"]


def test_every_invalid_setting_is_reported(settings_env: pytest.MonkeyPatch) -> None:
    """Check that load_settings reports all invalid settings at once, not just the first."""
    with pytest.raises(ValidationError) as error:
        settings_with(settings_env, PAGE_CACHE_SIZE="-1", RESERVED_PREFIX="browl")
    assert {e["loc"][0] for e in error.value.errors()} == {"PAGE_CACHE_SIZE", "RESERVED_PREFIX"}


def test_secret_is_not_shown(settings_env: pytest.MonkeyPatch) -> None:
    """Check that the OIDC client secret doesn't appear in the settings' repr."""
    assert "client-secret" not in repr(load_settings())


def test_settings_are_immutable(settings_env: pytest.MonkeyPatch) -> None:
    """Check that settings can't be changed after loading."""
    s = load_settings()
    with pytest.raises(ValidationError):
        s.page_cache_size = 5  # type: ignore[misc]
