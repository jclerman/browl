"""Tests for the settings module."""

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from browl.settings import UNTAGGED, Settings, load_settings

AUTH = {
    "OIDC_CLIENT_ID": "client-id",
    "OIDC_CLIENT_SECRET": "client-secret",
    "SEED_ADMINS": '["Admin@Example.org"]',
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """An environment with only the required settings, ignoring any the developer has set."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("BASE_URL", "https://example.org/onto")
    for name, value in AUTH.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def settings_with(env: pytest.MonkeyPatch, **values: str) -> Settings:
    """Load the settings after setting the given environment variables."""
    for name, value in values.items():
        env.setenv(name, value)
    return load_settings()


def test_defaults(env: pytest.MonkeyPatch) -> None:
    """Unset settings get their documented defaults."""
    s = load_settings()
    assert s.base_url == "https://example.org/onto"
    assert s.reserved_prefix == "_browl"
    assert s.name_properties == ["rdfs:label"]
    assert s.language_ranking == ["en", UNTAGGED]
    assert s.extra_prefixes == {}
    assert s.page_cache_size == 100
    assert s.app_jvm_max_heap == "1g"
    assert s.fuseki_url == "http://fuseki:3030"
    assert s.data_dir == Path("/data")
    assert s.auth_disabled is False


def test_base_url_is_required(env: pytest.MonkeyPatch) -> None:
    """BASE_URL has no default."""
    env.delenv("BASE_URL")
    with pytest.raises(ValidationError, match="base_url"):
        load_settings()


@pytest.mark.parametrize(
    ("value", "expected"),
    [("https://example.org/", "https://example.org"), ("http://example.org:8080/a/", "http://example.org:8080/a")],
)
def test_base_url_drops_trailing_slash(env: pytest.MonkeyPatch, value: str, expected: str) -> None:
    """BASE_URL is stored without a trailing slash."""
    assert settings_with(env, BASE_URL=value).base_url == expected


@pytest.mark.parametrize(
    "value", ["example.org", "ftp://example.org", "https://", "https://x.org/?a=1", "https://x.org/#a"]
)
def test_base_url_rejects_invalid(env: pytest.MonkeyPatch, value: str) -> None:
    """BASE_URL must be an absolute http(s) URL without query or fragment."""
    with pytest.raises(ValidationError, match="base_url"):
        settings_with(env, BASE_URL=value)


@pytest.mark.parametrize("value", ["_x", "_browl", "_a.b~c-d_e"])
def test_reserved_prefix_accepts_valid(env: pytest.MonkeyPatch, value: str) -> None:
    """A RESERVED_PREFIX of one URL-safe segment starting with '_' is accepted."""
    assert settings_with(env, RESERVED_PREFIX=value).reserved_prefix == value


@pytest.mark.parametrize("value", ["browl", "_", "_a/b", "/_browl", "_a b", "_a%20"])
def test_reserved_prefix_rejects_invalid(env: pytest.MonkeyPatch, value: str) -> None:
    """Any other RESERVED_PREFIX is rejected (O-3)."""
    with pytest.raises(ValidationError, match="reserved_prefix"):
        settings_with(env, RESERVED_PREFIX=value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('["en", "fr"]', ["en", "fr", UNTAGGED]),
        ('["EN-gb"]', ["en-gb", UNTAGGED]),
        ('["none", "en"]', [UNTAGGED, "en"]),
        ("[]", [UNTAGGED]),
    ],
)
def test_language_ranking(env: pytest.MonkeyPatch, value: str, expected: list[str]) -> None:
    """Tags are lowercased, and the untagged entry is appended unless placed explicitly (O-5)."""
    assert settings_with(env, LANGUAGE_RANKING=value).language_ranking == expected


@pytest.mark.parametrize("value", ['["en", "EN"]', '["en_US"]', '["toolonglanguage"]', '"en"'])
def test_language_ranking_rejects_invalid(env: pytest.MonkeyPatch, value: str) -> None:
    """Invalid or duplicate language tags are rejected."""
    with pytest.raises(ValidationError, match="language_ranking"):
        settings_with(env, LANGUAGE_RANKING=value)


def test_name_properties_from_json(env: pytest.MonkeyPatch) -> None:
    """NAME_PROPERTIES is read from JSON, in order (O-4)."""
    value = '["skos:prefLabel", "http://www.w3.org/2000/01/rdf-schema#label"]'
    expected = ["skos:prefLabel", "http://www.w3.org/2000/01/rdf-schema#label"]
    assert settings_with(env, NAME_PROPERTIES=value).name_properties == expected


@pytest.mark.parametrize("value", ["[]", '["rdfs:label", "rdfs:label"]'])
def test_name_properties_rejects_invalid(env: pytest.MonkeyPatch, value: str) -> None:
    """NAME_PROPERTIES can't be empty or repeat a property."""
    with pytest.raises(ValidationError, match="name_properties"):
        settings_with(env, NAME_PROPERTIES=value)


def test_extra_prefixes_from_json(env: pytest.MonkeyPatch) -> None:
    """EXTRA_PREFIXES is read from JSON, including the empty prefix (O-11)."""
    value = '{"ex": "http://example.org/", "": "urn:x:"}'
    assert settings_with(env, EXTRA_PREFIXES=value).extra_prefixes == {"ex": "http://example.org/", "": "urn:x:"}


@pytest.mark.parametrize("value", ['{"1x": "http://example.org/"}', '{"ex": "example.org/"}'])
def test_extra_prefixes_rejects_invalid(env: pytest.MonkeyPatch, value: str) -> None:
    """Invalid prefix names and relative namespaces are rejected."""
    with pytest.raises(ValidationError, match="extra_prefixes"):
        settings_with(env, EXTRA_PREFIXES=value)


@pytest.mark.parametrize(
    ("name", "value"),
    [("PAGE_CACHE_SIZE", "-1"), ("REASONER_TIMEOUT_S", "0"), ("APP_JVM_MAX_HEAP", "1 g"), ("REASONER_MAX_HEAP", "0g")],
)
def test_limits_reject_invalid(env: pytest.MonkeyPatch, name: str, value: str) -> None:
    """Negative sizes, a zero timeout and malformed heap sizes are rejected."""
    with pytest.raises(ValidationError, match=name.lower()):
        settings_with(env, **{name: value})


@pytest.mark.parametrize("missing", list(AUTH))
def test_auth_settings_required(env: pytest.MonkeyPatch, missing: str) -> None:
    """Each OIDC setting and SEED_ADMINS is required while authentication is on (O-6)."""
    env.delenv(missing)
    with pytest.raises(ValidationError, match=f"{missing}.* required unless AUTH_DISABLED"):
        load_settings()


def test_seed_admins_lowercased(env: pytest.MonkeyPatch) -> None:
    """Seed admin addresses are lowercased."""
    assert load_settings().seed_admins == ["admin@example.org"]


@pytest.mark.parametrize("value", ['["admin"]', '["a@b@c"]', '["a@b.org", "A@b.org"]'])
def test_seed_admins_rejects_invalid(env: pytest.MonkeyPatch, value: str) -> None:
    """Malformed or duplicate seed admin addresses are rejected."""
    with pytest.raises(ValidationError, match="seed_admins"):
        settings_with(env, SEED_ADMINS=value)


def test_auth_disabled_needs_no_auth_settings_and_warns(
    env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AUTH_DISABLED makes the auth settings optional and logs a warning (O-7)."""
    for name in AUTH:
        env.delenv(name)
    with caplog.at_level(logging.WARNING):
        s = settings_with(env, AUTH_DISABLED="true")
    assert s.auth_disabled is True
    assert "authentication is OFF" in caplog.text


def test_no_warning_with_auth(env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Nothing is logged when authentication is on."""
    with caplog.at_level(logging.WARNING):
        load_settings()
    assert caplog.text == ""


def test_secret_is_not_shown(env: pytest.MonkeyPatch) -> None:
    """The OIDC client secret doesn't appear in the settings' repr."""
    assert "client-secret" not in repr(load_settings())


def test_settings_are_immutable(env: pytest.MonkeyPatch) -> None:
    """Settings can't be changed after loading."""
    s = load_settings()
    with pytest.raises(ValidationError):
        s.page_cache_size = 5  # type: ignore[misc]
