"""Tests for reading the prefixes an ontology file declares."""

import logging
from pathlib import Path

import pytest

from browl import prefixes
from browl.prefixes import read_prefixes
from tests.conftest import DATA

EX = "http://example.org/"


def write(tmp_path: Path, name: str, content: str | bytes) -> Path:
    """Write a file, returning its path."""
    path = tmp_path / name
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    return path


def test_turtle(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Turtle's prefix directives, in either style, until the first statement; relative IRIs resolve against a base."""
    path = write(
        tmp_path,
        "o.ttl",
        "\ufeff# A comment\n"
        f"@prefix : <{EX}o#> .\n"
        f"@prefix obo:<http://purl.obolibrary.org/obo/>.  # no spaces\n"
        f"PREFIX sparql: <{EX}s/>\n"
        f"@base <{EX}base/> .\n"
        "@prefix rel: <rel/> .\n"
        "BASE <bad base>\n"
        "@prefix after: <x> .\n"
        f"<{EX}a> a <{EX}b> .\n"
        f"@prefix late: <{EX}late/> .\n",
    )
    assert read_prefixes(path) == {
        "": f"{EX}o#",
        "obo": "http://purl.obolibrary.org/obo/",
        "sparql": f"{EX}s/",
        "rel": f"{EX}base/rel/",
    }


def test_turtle_relative_without_base(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A relative namespace without a base isn't an IRI, so it's left out, with a warning."""
    path = write(tmp_path, "o.ttl", f"@prefix rel: <rel/> .\n@prefix ex: <{EX}> .\n")
    assert read_prefixes(path) == {"ex": EX}
    assert "The ontology's prefix 'rel' stands for 'rel/', not an absolute IRI" in caplog.text


def test_rdf_xml(tmp_path: Path) -> None:
    """RDF/XML's namespaces on its root element, with entities expanded; the default namespace is the empty prefix."""
    path = write(
        tmp_path,
        "o.rdf",
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE rdf:RDF [ <!ENTITY obo "http://purl.obolibrary.org/obo/"> ]>\n'
        f'<rdf:RDF xmlns="{EX}o#" xmlns:obo="&obo;" xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        f'  <rdf:Description xmlns:inner="{EX}inner/"/>\n'
        "</rdf:RDF>\n",
    )
    assert read_prefixes(path) == {
        "": f"{EX}o#",
        "obo": "http://purl.obolibrary.org/obo/",
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    }


def test_names_fixture() -> None:
    """The test ontology's prefixes are read."""
    assert read_prefixes(DATA / "names.ttl")["ex"] == "http://example.org/names#"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"<rdf:RDF xmlns:rdf='x'", "no root element in its first"),
        (b"not xml", "its XML doesn't parse"),
    ],
)
def test_rdf_xml_unreadable(tmp_path: Path, caplog: pytest.LogCaptureFixture, content: bytes, message: str) -> None:
    """A file whose root element isn't found has no prefixes, with a warning."""
    caplog.set_level(logging.WARNING)
    assert read_prefixes(write(tmp_path, "o.rdf", content)) == {}
    assert message in caplog.text


def test_rdf_xml_root_past_limit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the start of a file is read: a root element that starts later isn't found."""
    monkeypatch.setattr(prefixes, "HEAD_BYTES", 100)
    path = write(tmp_path, "o.rdf", f"<!-- {'x' * 200} -->\n<rdf:RDF xmlns:ex='{EX}'/>")
    assert read_prefixes(path) == {}
    assert "no root element in its first 100 bytes" in caplog.text


def test_missing_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A file that can't be read has no prefixes, with a warning."""
    assert read_prefixes(tmp_path / "missing.ttl") == {}
    assert "Couldn't read the ontology's prefixes from" in caplog.text


def test_turtle_base_keeps_trailing_hash(tmp_path: Path) -> None:
    """After a base, a namespace keeps its trailing "#" or "?", absolute or relative (M1)."""
    path = write(
        tmp_path,
        "o.ttl",
        f"@base <{EX}onto> .\n"
        f"@prefix : <{EX}onto#> .\n"
        "@prefix rel: <#> .\n"
        "@prefix sub: <sub#> .\n"
        "@prefix q: <q?> .\n"
        f"@prefix abs: <{EX}abs/> .\n",
    )
    assert read_prefixes(path) == {
        "": f"{EX}onto#",
        "rel": f"{EX}onto#",
        "sub": f"{EX}sub#",
        "q": f"{EX}q?",
        "abs": f"{EX}abs/",
    }


def test_turtle_base_unresolvable(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A namespace that doesn't resolve against a malformed base is left out with a warning, not an error (S1)."""
    path = write(tmp_path, "o.ttl", f"@base <http://[bad/> .\n@prefix rel: <foo#> .\n@prefix ex: <{EX}> .\n")
    assert read_prefixes(path) == {"ex": EX}
    assert "The ontology's prefix <foo#> doesn't resolve (" in caplog.text


@pytest.mark.parametrize(
    ("name", "syntax"),
    [
        ('x"onerror=alert(1)//', "ttl"),
        ("a/b", "ttl"),
        ("a#b", "ttl"),
        ("a?b", "ttl"),
        ("1a", "ttl"),
        ("é", "ttl"),
        ("http", "ttl"),
        ("HTTPS", "ttl"),
        ("javascript", "ttl"),
        ("http", "rdf"),
    ],
)
def test_bad_prefix_names(tmp_path: Path, caplog: pytest.LogCaptureFixture, name: str, syntax: str) -> None:
    """A declared name that isn't a prefix name, or is a URL scheme, is left out with a warning (S3)."""
    if syntax == "ttl":
        path = write(tmp_path, "o.ttl", f"@prefix {name}: <{EX}bad/> .\n@prefix ex: <{EX}> .\n")
    else:
        path = write(tmp_path, "o.rdf", f'<rdf:RDF xmlns:{name}="{EX}bad/" xmlns:ex="{EX}" xmlns:rdf="{EX}rdf#"/>')
    assert read_prefixes(path).keys() == ({"ex"} if syntax == "ttl" else {"ex", "rdf"})
    assert f"The ontology's prefix name {name!r} isn't allowed" in caplog.text


def test_warnings_limited(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Only the first few prefixes left out are logged, each value cut short (C3)."""
    long_iri = "rel/" + "x" * 1000
    lines = [f"@prefix p{n}: <{long_iri}> .\n" for n in range(prefixes._LOGGED_WARNINGS + 5)]
    assert read_prefixes(write(tmp_path, "o.ttl", "".join(lines))) == {}
    warnings = [record.getMessage() for record in caplog.records]
    assert len(warnings) == prefixes._LOGGED_WARNINGS + 1
    assert warnings[-1] == "More of the ontology's prefixes are left out; their warnings aren't logged"
    assert all(len(warning) < prefixes._LOGGED_CHARS + 100 for warning in warnings)
    assert f"'{long_iri[: prefixes._LOGGED_CHARS]}…'" in warnings[0]
