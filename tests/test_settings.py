"""Tests for the settings module."""

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from browl.settings import UNTAGGED, Settings, confirm_unique, load_settings

AUTH = {
    "OIDC_CLIENT_ID": "client-id",
    "OIDC_CLIENT_SECRET": "client-secret",
    "SEED_ADMINS": '["Admin@Example.org"]',
}


@pytest.mark.parametrize(
    ("values", "duplicates"),
    [
        pytest.param([], None, id="empty"),
        pytest.param(["a"], None, id="one"),
        pytest.param(["a", "b", "c"], None, id="all-unique"),
        pytest.param(["a", "A"], None, id="case-sensitive"),
        pytest.param(["a", "a"], "a", id="one-duplicate"),
        pytest.param(["a", "b", "a", "a", "c", "b"], "a, b", id="each-reported-once-in-order"),
        pytest.param(["a", "b", "b", "a"], "b, a", id="order-of-second-occurrence"),
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


def test_defaults(settings_env: pytest.MonkeyPatch) -> None:
    """Check that unset settings get their documented defaults."""
    s = load_settings()
    assert str(s.base_url) == "https://example.org/onto"
    assert s.reserved_prefix == "_browl"
    assert s.name_properties == ["rdfs:label"]
    assert s.language_ranking == ["en", UNTAGGED]
    assert s.extra_prefixes == {}
    assert s.page_cache_size == 100
    assert s.app_jvm_max_heap == "1g"
    assert str(s.fuseki_url) == "http://fuseki:3030/"
    assert s.data_dir == Path("/data")
    assert s.auth_disabled is False


def test_base_url_is_required(settings_env: pytest.MonkeyPatch) -> None:
    """Check that BASE_URL is required."""
    settings_env.delenv("BASE_URL")
    with pytest.raises(ValidationError, match="BASE_URL"):
        load_settings()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.org", "https://example.org/"),
        ("HTTPS://Example.ORG:443/onto/", "https://example.org/onto/"),
        ("https://bücher.example/onto/Bär", "https://xn--bcher-kva.example/onto/B%C3%A4r"),
    ],
)
def test_base_url_is_normalized(settings_env: pytest.MonkeyPatch, value: str, expected: str) -> None:
    """Check that BASE_URL is normalized as a URL: lowercase scheme and host, no default port, IDN and path encoded."""
    assert str(settings_with(settings_env, BASE_URL=value).base_url) == expected


@pytest.mark.parametrize(
    "value",
    ["example.org", "ftp://example.org", "https://", "https://ex ample.org", "https://x.org/?a=1", "https://x.org/#a"],
)
def test_base_url_rejects_invalid(settings_env: pytest.MonkeyPatch, value: str) -> None:
    """Check that BASE_URL must be an absolute http(s) URL without query or fragment."""
    with pytest.raises(ValidationError, match="BASE_URL"):
        settings_with(settings_env, BASE_URL=value)


@pytest.mark.parametrize("value", ["_x", "_browl", "_a.b~c-d_e"])
def test_reserved_prefix_accepts_valid(settings_env: pytest.MonkeyPatch, value: str) -> None:
    """Check that a RESERVED_PREFIX of one URL-safe segment starting with '_' is accepted."""
    assert settings_with(settings_env, RESERVED_PREFIX=value).reserved_prefix == value


@pytest.mark.parametrize("value", ["browl", "_", "_a/b", "/_browl", "_a b", "_a%20"])
def test_reserved_prefix_rejects_invalid(settings_env: pytest.MonkeyPatch, value: str) -> None:
    """Check that any other RESERVED_PREFIX is rejected (O-3)."""
    with pytest.raises(ValidationError, match="RESERVED_PREFIX"):
        settings_with(settings_env, RESERVED_PREFIX=value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('["en", "fr"]', ["en", "fr", UNTAGGED]),
        ('["EN-gb"]', ["en-gb", UNTAGGED]),
        ('["zh-Hant-TW", "iw"]', ["zh-hant-tw", "iw", UNTAGGED]),
        ('["none", "en"]', [UNTAGGED, "en"]),
        ("[]", [UNTAGGED]),
    ],
)
def test_language_ranking(settings_env: pytest.MonkeyPatch, value: str, expected: list[str]) -> None:
    """Check that tags are lowercased, and the untagged entry is appended unless placed explicitly (O-5)."""
    assert settings_with(settings_env, LANGUAGE_RANKING=value).language_ranking == expected


@pytest.mark.parametrize(
    "value",
    ['["en", "EN"]', '["en_US"]', '["toolonglanguage"]', '["xx"]', '["en-yy"]', '["en-latm"]', '["en-gb-foo"]', '"en"'],
)
def test_language_ranking_rejects_invalid(settings_env: pytest.MonkeyPatch, value: str) -> None:
    """Check that invalid or duplicate language tags are rejected."""
    with pytest.raises(ValidationError, match="LANGUAGE_RANKING"):
        settings_with(settings_env, LANGUAGE_RANKING=value)


def test_name_properties_from_json(settings_env: pytest.MonkeyPatch) -> None:
    """Check that NAME_PROPERTIES is read from JSON, in order (O-4)."""
    value = '["skos:prefLabel", "http://www.w3.org/2000/01/rdf-schema#label"]'
    expected = ["skos:prefLabel", "http://www.w3.org/2000/01/rdf-schema#label"]
    assert settings_with(settings_env, NAME_PROPERTIES=value).name_properties == expected


@pytest.mark.parametrize("value", ["[]", '["rdfs:label", "rdfs:label"]'])
def test_name_properties_rejects_invalid(settings_env: pytest.MonkeyPatch, value: str) -> None:
    """Check that NAME_PROPERTIES can't be empty or repeat a property."""
    with pytest.raises(ValidationError, match="NAME_PROPERTIES"):
        settings_with(settings_env, NAME_PROPERTIES=value)


def test_extra_prefixes_from_json(settings_env: pytest.MonkeyPatch) -> None:
    """Check that EXTRA_PREFIXES is read from JSON, including the empty prefix (O-11)."""
    value = '{"ex": "http://example.org/", "": "urn:x:"}'
    assert settings_with(settings_env, EXTRA_PREFIXES=value).extra_prefixes == {
        "ex": "http://example.org/",
        "": "urn:x:",
    }


@pytest.mark.parametrize("value", ['{"1x": "http://example.org/"}', '{"ex": "example.org/"}'])
def test_extra_prefixes_rejects_invalid(settings_env: pytest.MonkeyPatch, value: str) -> None:
    """Check that invalid prefix names and relative namespaces are rejected."""
    with pytest.raises(ValidationError, match="EXTRA_PREFIXES"):
        settings_with(settings_env, EXTRA_PREFIXES=value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PAGE_CACHE_SIZE", "-1"),
        ("REASONER_TIMEOUT_S", "0"),
        ("APP_JVM_MAX_HEAP", "1 g"),
        ("REASONER_MAX_HEAP", "0g"),
        ("APP_JVM_MAX_HEAP", "1024"),
    ],
)
def test_limits_reject_invalid(settings_env: pytest.MonkeyPatch, name: str, value: str) -> None:
    """Check that negative sizes, a zero timeout and malformed heap sizes are rejected."""
    with pytest.raises(ValidationError, match=name):
        settings_with(settings_env, **{name: value})


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


def test_seed_admins_lowercased(settings_env: pytest.MonkeyPatch) -> None:
    """Check that seed admin addresses are lowercased."""
    assert load_settings().seed_admins == ["admin@example.org"]


@pytest.mark.parametrize("value", ['["admin"]', '["a@b@c"]', '["a@b.org", "A@b.org"]'])
def test_seed_admins_rejects_invalid(settings_env: pytest.MonkeyPatch, value: str) -> None:
    """Check that malformed or duplicate seed admin addresses are rejected."""
    with pytest.raises(ValidationError, match="SEED_ADMINS"):
        settings_with(settings_env, SEED_ADMINS=value)


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


@pytest.mark.parametrize("name", ["page_cache_size", "Page_Cache_Size"])
def test_only_uppercase_env_names_are_read(settings_env: pytest.MonkeyPatch, name: str) -> None:
    """Check that an environment variable whose name isn't all uppercase is ignored."""
    settings_env.setenv(name, "7")
    assert load_settings().page_cache_size == 100


def test_uppercase_env_name_wins_over_other_casings(settings_env: pytest.MonkeyPatch) -> None:
    """Check that the uppercase variable is used even when a lowercase one is also set."""
    settings_env.setenv("page_cache_size", "7")
    settings_env.setenv("PAGE_CACHE_SIZE", "8")
    assert load_settings().page_cache_size == 8


@pytest.mark.parametrize(
    ("values", "invalid"),
    [
        pytest.param({"PAGE_CACHE_SIZE": "-1"}, {"PAGE_CACHE_SIZE"}, id="negative-cache-size"),
        pytest.param({"RESERVED_PREFIX": "browl"}, {"RESERVED_PREFIX"}, id="prefix-without-underscore"),
        pytest.param({"FUSEKI_URL": "fuseki:3030"}, {"FUSEKI_URL"}, id="relative-fuseki-url"),
        pytest.param(
            {"PAGE_CACHE_SIZE": "-1", "RESERVED_PREFIX": "browl"},
            {"PAGE_CACHE_SIZE", "RESERVED_PREFIX"},
            id="all-errors-reported",
        ),
    ],
)
def test_load_settings_rejects_invalid_settings(
    settings_env: pytest.MonkeyPatch, values: dict[str, str], invalid: set[str]
) -> None:
    """Check that load_settings raises ValidationError naming exactly the invalid settings."""
    with pytest.raises(ValidationError) as error:
        settings_with(settings_env, **values)
    assert {e["loc"][0] for e in error.value.errors()} == invalid


def test_secret_is_not_shown(settings_env: pytest.MonkeyPatch) -> None:
    """Check that the OIDC client secret doesn't appear in the settings' repr."""
    assert "client-secret" not in repr(load_settings())


def test_settings_are_immutable(settings_env: pytest.MonkeyPatch) -> None:
    """Check that settings can't be changed after loading."""
    s = load_settings()
    with pytest.raises(ValidationError):
        s.page_cache_size = 5  # type: ignore[misc]
