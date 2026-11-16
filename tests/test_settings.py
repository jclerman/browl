"""Tests for the settings module."""

import logging
import re
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


@pytest.mark.parametrize("value", ["_x", "_browl", "_a.b~c-d_e"])
def test_reserved_prefix_accepts_valid(settings_env: pytest.MonkeyPatch, value: str) -> None:
    """Check that a RESERVED_PREFIX of one URL-safe segment starting with '_' is accepted."""
    assert settings_with(settings_env, RESERVED_PREFIX=value).reserved_prefix == value


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


def test_name_properties_from_json(settings_env: pytest.MonkeyPatch) -> None:
    """Check that NAME_PROPERTIES is read from JSON, in order (O-4)."""
    value = '["skos:prefLabel", "http://www.w3.org/2000/01/rdf-schema#label"]'
    expected = ["skos:prefLabel", "http://www.w3.org/2000/01/rdf-schema#label"]
    assert settings_with(settings_env, NAME_PROPERTIES=value).name_properties == expected


def test_extra_prefixes_from_json(settings_env: pytest.MonkeyPatch) -> None:
    """Check that EXTRA_PREFIXES is read from JSON, including the empty prefix (O-11)."""
    value = '{"ex": "http://example.org/", "": "urn:x:"}'
    assert settings_with(settings_env, EXTRA_PREFIXES=value).extra_prefixes == {
        "ex": "http://example.org/",
        "": "urn:x:",
    }


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


INVALID_TAG = "invalid language tags"
DUPLICATES = "duplicate entries"
HEAP_SIZE = "must be a Java heap size"
PREFIX_RULE = "must start with '_' and be one path segment"
NOT_EMAIL = "not email addresses"


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        pytest.param("BASE_URL", "example.org", "relative URL without a base", id="base-url-relative"),
        pytest.param("BASE_URL", "ftp://example.org", "scheme should be 'http' or 'https'", id="base-url-ftp"),
        pytest.param("BASE_URL", "https://", "empty host", id="base-url-no-host"),
        pytest.param("BASE_URL", "https://ex ample.org", "invalid international domain name", id="base-url-bad-host"),
        pytest.param("BASE_URL", "https://x.org/?a=1", "must not have a query string or fragment", id="base-url-query"),
        pytest.param(
            "BASE_URL", "https://x.org/#a", "must not have a query string or fragment", id="base-url-fragment"
        ),
        pytest.param("FUSEKI_URL", "fuseki:3030", "scheme should be 'http' or 'https'", id="fuseki-url-no-scheme"),
        pytest.param("RESERVED_PREFIX", "browl", PREFIX_RULE, id="prefix-no-underscore"),
        pytest.param("RESERVED_PREFIX", "_", PREFIX_RULE, id="prefix-underscore-only"),
        pytest.param("RESERVED_PREFIX", "_a/b", PREFIX_RULE, id="prefix-two-segments"),
        pytest.param("RESERVED_PREFIX", "/_browl", PREFIX_RULE, id="prefix-leading-slash"),
        pytest.param("RESERVED_PREFIX", "_a b", PREFIX_RULE, id="prefix-space"),
        pytest.param("RESERVED_PREFIX", "_a%20", PREFIX_RULE, id="prefix-percent"),
        pytest.param("LANGUAGE_RANKING", '["en", "EN"]', f"{DUPLICATES}: en$", id="lang-duplicate-by-case"),
        pytest.param("LANGUAGE_RANKING", '["en_US"]', f"{INVALID_TAG}: en_us$", id="lang-underscore"),
        pytest.param("LANGUAGE_RANKING", '["toolonglanguage"]', f"{INVALID_TAG}: toolonglanguage$", id="lang-too-long"),
        pytest.param("LANGUAGE_RANKING", '["xx"]', f"{INVALID_TAG}: xx$", id="lang-unregistered-language"),
        pytest.param("LANGUAGE_RANKING", '["en-yy"]', f"{INVALID_TAG}: en-yy$", id="lang-unregistered-region"),
        pytest.param("LANGUAGE_RANKING", '["en-latm"]', f"{INVALID_TAG}: en-latm$", id="lang-unregistered-script"),
        pytest.param("LANGUAGE_RANKING", '["en-gb-foo"]', f"{INVALID_TAG}: en-gb-foo$", id="lang-unregistered-variant"),
        pytest.param("LANGUAGE_RANKING", '"en"', "should be a valid list", id="lang-not-a-list"),
        pytest.param("NAME_PROPERTIES", "[]", "at least 1 item", id="names-empty"),
        pytest.param(
            "NAME_PROPERTIES", '["rdfs:label", "rdfs:label"]', f"{DUPLICATES}: rdfs:label$", id="names-duplicate"
        ),
        pytest.param(
            "EXTRA_PREFIXES", '{"1x": "http://example.org/"}', "invalid prefix name '1x'", id="prefixes-bad-name"
        ),
        pytest.param(
            "EXTRA_PREFIXES",
            '{"ex": "example.org/"}',
            "namespace for 'ex' must be an absolute IRI",
            id="prefixes-relative",
        ),
        pytest.param("PAGE_CACHE_SIZE", "-1", "greater than or equal to 0", id="cache-size-negative"),
        pytest.param("REASONER_TIMEOUT_S", "0", "greater than 0", id="timeout-zero"),
        pytest.param("APP_JVM_MAX_HEAP", "1 g", HEAP_SIZE, id="heap-space"),
        pytest.param("APP_JVM_MAX_HEAP", "1024", HEAP_SIZE, id="heap-no-unit"),
        pytest.param("REASONER_MAX_HEAP", "0g", HEAP_SIZE, id="heap-zero"),
        pytest.param("SEED_ADMINS", '["admin"]', f"{NOT_EMAIL}: admin$", id="admins-no-at"),
        pytest.param("SEED_ADMINS", '["a@b@c"]', f"{NOT_EMAIL}: a@b@c$", id="admins-two-ats"),
        pytest.param("SEED_ADMINS", '["a@b.org", "A@b.org"]', f"{DUPLICATES}: a@b.org$", id="admins-duplicate-by-case"),
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
