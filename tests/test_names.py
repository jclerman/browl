"""Tests for choosing among name values."""

from pathlib import Path

import pytest

from browl.names import BUILTIN_PREFIXES, best_values, expand_curie, language_rank, name_property_iris, normalize
from browl.rdf import RDF_LANG_STRING, XSD_STRING, Literal
from browl.settings import UNTAGGED
from tests.helpers import make_settings

DEFAULT = ["en", UNTAGGED]
UNTAGGED_FIRST = ["fr", UNTAGGED, "en-gb", "zh-hant"]


def text(value: str, language: str | None = None) -> Literal:
    """Return a string literal, language-tagged if a language is given."""
    if language is None:
        return Literal(value, XSD_STRING)
    return Literal(value, RDF_LANG_STRING, language)


@pytest.mark.parametrize(
    ("language", "ranking", "expected"),
    [
        ("en", DEFAULT, 0),
        ("EN-GB", DEFAULT, 0),  # case-insensitive, and falls back to `en`
        (None, DEFAULT, 1),
        ("fr", DEFAULT, 0.5),  # unmatched: just before `none`, which is last
        ("en-gb", UNTAGGED_FIRST, 2),
        ("en-us", UNTAGGED_FIRST, 4),  # unmatched, `none` isn't last: after everything
        (None, UNTAGGED_FIRST, 1),
        ("zh-hant-tw", UNTAGGED_FIRST, 3),
        ("fr-x-private", UNTAGGED_FIRST, 0),  # the singleton `x` is dropped with the subtag after it
        ("de", [], 0),
    ],
)
def test_language_rank(language: str | None, ranking: list[str], expected: float) -> None:
    """A tag ranks by its lowercased lookup fallback; an unmatched tag just before a last `none`, else last."""
    assert language_rank(language, ranking) == expected


def test_best_values() -> None:
    """The values of the best language come back, deduplicated and in lexical order."""
    values = [text("b", "en"), text("a", "en-gb"), text("b", "en"), text("z"), text("c", "fr")]
    assert best_values(values, DEFAULT) == ["a", "b"]
    assert best_values([text("z"), text("c", "fr")], DEFAULT) == ["c"]
    assert best_values([text("z"), text("c", "fr")], ["fr", UNTAGGED]) == ["c"]
    assert best_values([text("z"), text("c", "de")], ["fr", UNTAGGED]) == ["c"]
    assert best_values([], DEFAULT) == []


def test_best_values_same_value_in_two_languages() -> None:
    """A value in several languages ranks by its best one."""
    assert best_values([text("x", "fr"), text("x", "en"), text("y", "en")], DEFAULT) == ["x", "y"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("rdfs:label", "http://www.w3.org/2000/01/rdf-schema#label"),
        ("ex:name", "http://example.org/name"),
        ("skos:prefLabel", "http://www.w3.org/2004/02/skos/core#prefLabel"),
        ("http://example.org/p", "http://example.org/p"),
        ("urn:x:p", "urn:x:p"),
        ("label", "label"),
    ],
)
def test_expand_curie(name: str, expected: str) -> None:
    """A CURIE with a known prefix is expanded; anything else is left as it is."""
    prefixes = BUILTIN_PREFIXES | {"ex": "http://example.org/", "http": "http://wrong/"}
    assert expand_curie(name, prefixes) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Café", "cafe"),
        ("CAFÉ", "cafe"),  # a combining accent
        ("Straße", "strasse"),  # case folding, not just lowercasing
        ("İstanbul", "istanbul"),  # the dot above, added by folding, is removed too
        ("\ufb01ne \uff12", "fine 2"),  # compatibility forms: a ligature, a full-width digit
        ("餃子", "餃子"),
    ],
)
def test_normalize(text: str, expected: str) -> None:
    """A value is normalized by NFKD, without accents, case folded (§2.6)."""
    assert normalize(text) == expected


def test_name_property_iris(data_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Name properties are expanded with the built-in and configured prefixes; repeats and non-IRIs are left out."""
    settings = make_settings(
        NAME_PROPERTIES=["skos:prefLabel", "ex:name", "http://example.org/name", "rdfs:label", "nope"],
        EXTRA_PREFIXES={"ex": "http://example.org/"},
    )
    assert name_property_iris(settings) == [
        "http://www.w3.org/2004/02/skos/core#prefLabel",
        "http://example.org/name",
        "http://www.w3.org/2000/01/rdf-schema#label",
    ]
    assert "The name property 'nope' isn't an IRI or a known CURIE" in caplog.text
