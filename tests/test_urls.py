"""Tests for the IRI → URL mapping (DESIGN.md §2.3)."""

import urllib.parse

import pytest
from pydantic import HttpUrl

from browl.urls import EntityURL, Rule, URLMapper, entity_url

BASE = HttpUrl("http://example.com/")
PREFIX = "_browl"


def url(iri: str, base: str | HttpUrl = BASE, prefix: str = PREFIX) -> EntityURL:
    """Return an IRI's URL on a site at `base`."""
    return entity_url(iri, HttpUrl(base) if isinstance(base, str) else base, prefix)


def fallback(iri: str, prefix: str = PREFIX, reserved: bool = False) -> EntityURL:
    """Return the fallback URL of an IRI."""
    return EntityURL(f"/{prefix}/iri?u={urllib.parse.quote(iri, safe='')}", None, Rule.FALLBACK, reserved)


@pytest.mark.parametrize(
    ("iri", "expected"),
    [
        ("http://example.com/onto/Foo", ("/onto/Foo", None)),
        ("http://example.com/", ("/", None)),
        ("http://example.com", ("/", None)),  # an empty path is `/`
        ("http://example.com/onto#Foo", ("/onto", "Foo")),
        ("http://example.com/#Foo", ("/", "Foo")),
        ("http://EXAMPLE.com/onto/Foo", ("/onto/Foo", None)),  # the host is compared case-insensitively
        ("HTTP://example.com/onto/Foo", ("/onto/Foo", None)),  # and the scheme
        ("http://example.com:80/onto/Foo", ("/onto/Foo", None)),  # the default port
        ("http://example.com:/onto/Foo", ("/onto/Foo", None)),  # an empty port
        ("https://example.com:443/onto/Foo", ("/onto/Foo", None)),
        ("http://example.com/onto/a%2Fb", ("/onto/a%2Fb", None)),  # percent-escapes are kept
        ("http://example.com/Rés#Ünï", ("/R%C3%A9s", "%C3%9Cn%C3%AF")),  # non-ASCII path and fragment, in URI form
        ("http://example.com/a b|c", ("/a%20b|c", None)),  # ASCII a browser escapes in a path is escaped
        ("http://example.com/a#b c", ("/a", "b%20c")),
        ("http://example.com/_browlish", ("/_browlish", None)),  # not the reserved segment
        ("http://example.com/x/_browl", ("/x/_browl", None)),
    ],
)
def test_native(iri: str, expected: tuple[str, str | None]) -> None:
    """An IRI under `BASE_URL` gets its own path, and keeps its fragment."""
    assert url(iri) == EntityURL(*expected, Rule.NATIVE)


def test_native_http_and_https() -> None:
    """The `http` and `https` forms of a native IRI share a URL (U-15), whichever scheme `BASE_URL` has."""
    for base in ["http://example.com/", "https://example.com/"]:
        assert url("http://example.com/x#y", base) == url("https://example.com/x#y", base)
        assert url("https://example.com/x", base) == EntityURL("/x", None, Rule.NATIVE)


@pytest.mark.parametrize(
    ("base", "iri", "path"),
    [
        ("http://example.com/browl/", "http://example.com/browl/onto/Foo", "/onto/Foo"),
        ("http://example.com/browl/", "http://example.com/browl/", "/"),
        ("http://example.com/browl/", "http://example.com/browl", "/"),
        ("http://example.com/browl", "http://example.com/browl/x", "/x"),
        ("http://localhost:8000/", "http://localhost:8000/x", "/x"),
        ("http://localhost:8000/", "https://localhost:8000/x", "/x"),
        ("http://[::1]:8000/", "http://[::1]:8000/x", "/x"),  # an IP literal host, natively
        ("http://bücher.example/", "http://BÜCHER.example/x", "/x"),  # a non-ASCII host, any case
        ("http://xn--bcher-kva.example/", "http://bücher.example/x", "/x"),
        ("http://example.com/Rés/", "http://example.com/Rés/x", "/x"),  # a non-ASCII path, percent-encoded alike
        ("http://example.com/R%C3%A9s/", "http://example.com/Rés/x", "/x"),
    ],
)
def test_native_base(base: str, iri: str, path: str) -> None:
    """IRIs are compared with `BASE_URL` in URI form, its path left off theirs."""
    assert url(iri, base) == EntityURL(path, None, Rule.NATIVE)


@pytest.mark.parametrize(
    ("base", "iri"),
    [
        ("http://example.com/browl/", "http://example.com/browlish/x"),  # not under the path
        ("http://example.com/browl/", "http://example.com/BROWL/x"),  # the path is compared exactly
        ("http://example.com/Rés/", "http://example.com/r%C3%A9s/x"),
        ("http://example.com/R%C3%A9s/", "http://example.com/R%c3%a9s/x"),
        ("http://localhost:8000/", "http://localhost/x"),  # another port
        ("http://example.com/", "http://www.example.com/x"),  # another host
        ("http://example.com/", "http://example.com.org/x"),
    ],
)
def test_not_native(base: str, iri: str) -> None:
    """An IRI not under `BASE_URL` isn't native."""
    assert url(iri, base).rule != Rule.NATIVE


@pytest.mark.parametrize(
    ("iri", "expected"),
    [
        ("http://example.org/ONTO/x", ("/example.org/ONTO/x", None)),
        ("https://example.org/ONTO/x", ("/example.org/ONTO/x", None)),  # the scheme is dropped
        ("http://example.org/onto#Foo", ("/example.org/onto", "Foo")),
        ("http://example.org", ("/example.org/", None)),
        ("http://Example.ORG/x", ("/example.org/x", None)),  # the host is normalized as BASE_URL's is
        ("http://bücher.example/Rés#é", ("/xn--bcher-kva.example/R%C3%A9s", "%C3%A9")),  # in punycode
        ("http://example.org:80/x", ("/example.org/x", None)),  # the default port is no port
        ("https://example.org:443/x", ("/example.org/x", None)),
        ("http://127.0.0.1/x", ("/127.0.0.1/x", None)),
    ],
)
def test_proxied(iri: str, expected: tuple[str, str | None]) -> None:
    """Any other plain `http` or `https` IRI gets `/{host}{path}`, and keeps its fragment."""
    assert url(iri) == EntityURL(*expected, Rule.PROXIED)


@pytest.mark.parametrize(
    "iri",
    [
        "urn:uuid:6e8bc430-9c3a-11d9-9669-0800200c9a66",
        "urn:isbn:0451450523#x",
        "tag:example.org,2026:onto#Foo",
        "mailto:someone@example.org",
        "ftp://example.org/x",
        "file:///etc/passwd",
        "http:x",  # no authority
        "http:///x",  # an empty host
        "http://example.org:8080/x",  # a port
        "http://example.com:8080/x",  # BASE_URL's host, another port
        "https://example.org:80/x",  # another scheme's default port
        "http://example.org:x/x",  # not a port
        "http://example.org:8080",
        "http://example.org/x?",  # an empty query string
        "http://example.org/x?a=1",  # a query string
        "http://example.org/x?a=1#y",
        "http://example.com/x?a=1",  # natively too
        "http://user@example.org/x",  # user info
        "http://user:pw@example.com/x",
        "http://example.org/x#",  # an empty fragment
        "http://example.com/x#",
        "http://[::1]/x",  # an IP literal, not natively
        "http://[::1/x",
        "http://[::1]x/x",
        "http://exa mple.org/x",  # not a host
        "http://example.org/a/../b",  # a dot segment, which a browser would resolve
        "http://example.org/a/./b",
        "http://example.org/a/%2E%2e/b",
        "http://example.com/..",
        "http://example.com//x",  # a native path a browser would read as another host (`//x`)
        "http://example.com//evil.com/x",
        "relative/x",
        "",
    ],
)
def test_fallback(iri: str) -> None:
    """Anything else gets `/{RESERVED_PREFIX}/iri?u={percent-encoded IRI}`, its fragment in the query."""
    assert url(iri) == fallback(iri)


def test_fallback_protocol_relative_under_base_path() -> None:
    """A native URL path that would start with `//`, relative to `BASE_URL`'s path, gets the fallback."""
    assert url("http://example.com/sub//evil.com/x", "http://example.com/sub") == fallback(
        "http://example.com/sub//evil.com/x"
    )
    assert url("http://example.com/sub/x//y", "http://example.com/sub") == EntityURL("/x//y", None, Rule.NATIVE)


def test_fallback_encoding() -> None:
    """The fallback URL percent-encodes the whole IRI, non-ASCII as UTF-8, under the configured prefix."""
    assert url("urn:x:é#a b", prefix="_site") == EntityURL("/_site/iri?u=urn%3Ax%3A%C3%A9%23a%20b", None, Rule.FALLBACK)


@pytest.mark.parametrize(
    ("iri", "base"),
    [
        ("http://example.com/_browl", BASE),
        ("http://example.com/_browl/admin", BASE),
        ("https://example.com/_browl/x#y", BASE),
        ("http://example.com/%5Fbrowl/x", BASE),  # percent-encoded, as a router decodes it
        ("http://example.com/sub/_browl/x", HttpUrl("http://example.com/sub/")),  # under BASE_URL's path
        ("http://_browl/x", BASE),  # proxied: a host that starts with the prefix
        ("http://_BROWL/x", BASE),
    ],
)
def test_reserved_prefix(iri: str, base: HttpUrl) -> None:
    """An IRI whose native or proxied URL would be under the reserved prefix gets the fallback, marked reserved."""
    assert url(iri, base) == fallback(iri, reserved=True)


def test_reserved_prefix_configured() -> None:
    """The collision is with the configured prefix."""
    assert url("http://example.com/_browl/x", prefix="_site").rule == Rule.NATIVE
    assert url("http://example.com/_site/x", prefix="_site") == fallback(
        "http://example.com/_site/x", "_site", reserved=True
    )
    assert url("http://example.com/sub/_browl/x").rule == Rule.NATIVE  # not at the root


def test_mapper_matches_function() -> None:
    """A `URLMapper` maps IRIs as `entity_url` does, for many IRIs with one `BASE_URL`."""
    mapper = URLMapper(BASE, PREFIX)
    for iri in ["http://example.com/x", "http://example.org/x#y", "urn:x"]:
        assert mapper(iri) == url(iri)
