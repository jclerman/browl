"""The IRI → URL mapping (DESIGN.md §2.3): the path and fragment of each entity's page on the site.

Paths are relative to `BASE_URL`, and in URI form, as a link's `href` or a request's raw path has them: an IRI's
non-ASCII characters, and the ASCII ones a browser would escape, are percent-encoded as UTF-8 (RFC 3987 §3.1), and its
existing percent-escapes are kept as they are.
"""

import functools
import re
import urllib.parse
from dataclasses import dataclass
from enum import StrEnum

from pydantic import HttpUrl
from pydantic_core import Url, ValidationError

FALLBACK_PATH = "iri"
"""The fallback route's path under the reserved prefix (DESIGN.md §2.13)."""

_DEFAULT_PORTS = {"http": 80, "https": 443}

# RFC 3986 appendix B, with the scheme required; DOTALL, so a newline can't end a part early.
_IRI = re.compile(
    r"(?P<scheme>[^:/?#]+):(?://(?P<authority>[^/?#]*))?(?P<path>[^?#]*)(?P<query>\?[^#]*)?(?:#(?P<fragment>.*))?",
    re.DOTALL,
)

# The printable ASCII characters a browser escapes in a URL's path (WHATWG URL, path percent-encode set; and `\`, which
# a browser would read as `/`) and in its fragment (fragment percent-encode set). Everything else in printable ASCII
# stays as it is, `%` included; control and non-ASCII characters are escaped.
_ESCAPED_IN_PATH = ' "#<>?`{}\\'
_ESCAPED_IN_FRAGMENT = ' "<>`'
_ALWAYS_SAFE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-~"  # never escaped by `quote`
_PATH_SAFE = "".join(chr(c) for c in range(0x21, 0x7F) if chr(c) not in _ESCAPED_IN_PATH + _ALWAYS_SAFE)
_FRAGMENT_SAFE = "".join(chr(c) for c in range(0x21, 0x7F) if chr(c) not in _ESCAPED_IN_FRAGMENT + _ALWAYS_SAFE)
_PATH_AS_IS = re.compile(f"[{re.escape(_ALWAYS_SAFE + _PATH_SAFE)}]*")
_DOT_SEGMENT = re.compile(r"(?:\.|%2e){1,2}", re.IGNORECASE)


class Rule(StrEnum):
    """Which of DESIGN.md §2.3's rules gave an entity's URL."""

    NATIVE = "native"
    PROXIED = "proxied"
    FALLBACK = "fallback"


@dataclass(frozen=True)
class EntityURL:
    """An entity's URL on the site.

    Attributes:
        path: The URL's path, from the site's root (`BASE_URL`), in URI form. For a fallback URL, the path and query
            string: `/{RESERVED_PREFIX}/iri?u={the percent-encoded IRI}`.
        fragment: The fragment of a hash IRI's URL, in URI form; None for any other, and for every fallback URL, whose
            query holds the whole IRI.
        rule: The rule that gave the URL.
        reserved: Whether the IRI got the fallback URL because the URL another rule gives it would be under the reserved
            prefix (O-3).
    """

    path: str
    fragment: str | None
    rule: Rule
    reserved: bool = False


class URLMapper:
    """Maps IRIs to their URLs on a site (`entity_url`), with `BASE_URL` parsed once.

    Args:
        base_url: The site's `BASE_URL`, normalized as Pydantic normalizes it.
        reserved_prefix: The first path segment of the service's own pages (`RESERVED_PREFIX`).
    """

    def __init__(self, base_url: HttpUrl, reserved_prefix: str) -> None:
        """Keep `BASE_URL`'s host, port and path, as an IRI's are compared with them."""
        self.reserved_prefix = reserved_prefix
        self._host = base_url.host
        self._port = None if base_url.port == _DEFAULT_PORTS[base_url.scheme] else base_url.port
        self._path = base_url.path.rstrip("/") if base_url.path else ""

    def __call__(self, iri: str) -> EntityURL:
        """Return an IRI's URL on the site (DESIGN.md §2.3), by the first rule that applies.

        1. Native: an `http` or `https` IRI whose host and port are `BASE_URL`'s, and whose path is under `BASE_URL`'s,
           gets that path, less `BASE_URL`'s.
        2. Proxied: any other `http` or `https` IRI, unless it has user info or a port other than its scheme's default,
           gets `/{host}{path}`; the host is normalized as `BASE_URL`'s is (lowercased, non-ASCII in punycode).
        3. Fallback: any other IRI gets `/{RESERVED_PREFIX}/iri?u={the percent-encoded IRI}`. So do IRIs with a query
           string, even an empty one; with an empty fragment, which a URL can't be told apart from no fragment by; with
           an IP literal host (`[::1]`) unless native; whose path has a `.` or `..` segment, which a browser would
           resolve away; whose native URL path would start with `//`, which a browser would read as another host; with a
           host that isn't valid; and whose native or proxied URL would be under the reserved prefix: its first path
           segment, percent-decoded as a router decodes it, is `RESERVED_PREFIX`.

        IRIs are compared with `BASE_URL` in URI form (RFC 3987 §3.1): the scheme and host case-insensitively, after the
        host is normalized, and the path exactly. `http` and `https` aren't told apart, so the two forms of an IRI share
        a URL (U-15). An empty path is read as `/`. A hash IRI's fragment is kept apart, in URI form; the rules apply to
        the rest of it.
        """
        match = _IRI.fullmatch(iri)
        if match is None or (authority := match["authority"]) is None or match["query"] is not None:
            return self._fallback(iri)
        scheme = match["scheme"].lower()
        fragment = match["fragment"]
        if scheme not in _DEFAULT_PORTS or fragment == "" or "@" in authority:
            return self._fallback(iri)
        host, port = _split_authority(authority, _DEFAULT_PORTS[scheme])
        if host is None or port == -1:
            return self._fallback(iri)
        path = _to_uri(match["path"], _PATH_SAFE) or "/"
        if any(_DOT_SEGMENT.fullmatch(segment) for segment in path.split("/")):
            return self._fallback(iri)
        if host == self._host and port == self._port and (path == self._path or path.startswith(self._path + "/")):
            url_path, rule = path[len(self._path) :] or "/", Rule.NATIVE
            if url_path.startswith("//"):  # a protocol-relative URL, to another host
                return self._fallback(iri)
        elif port is None and not host.startswith("["):
            url_path, rule = f"/{host}{path}", Rule.PROXIED
        else:
            return self._fallback(iri)
        if urllib.parse.unquote(url_path).split("/", 2)[1] == self.reserved_prefix:
            return self._fallback(iri, reserved=True)
        return EntityURL(url_path, None if fragment is None else _to_uri(fragment, _FRAGMENT_SAFE), rule)

    def _fallback(self, iri: str, reserved: bool = False) -> EntityURL:
        path = f"/{self.reserved_prefix}/{FALLBACK_PATH}?u={urllib.parse.quote(iri, safe='')}"
        return EntityURL(path, None, Rule.FALLBACK, reserved)


def entity_url(iri: str, base_url: HttpUrl, reserved_prefix: str) -> EntityURL:
    """Return an IRI's URL on a site served at `base_url` with a reserved prefix (`URLMapper`)."""
    return URLMapper(base_url, reserved_prefix)(iri)


def _split_authority(authority: str, default_port: int) -> tuple[str | None, int | None]:
    """Return an authority's normalized host and port.

    The host is None if it isn't a valid one. The port is None if absent, empty or the scheme's default; -1 if it isn't
    a number.
    """
    if authority.startswith("["):  # an IP literal, whose address has colons
        end = authority.find("]") + 1
        host, rest = authority[:end], authority[end:]
        if not end or (rest and not rest.startswith(":")):
            return None, -1
        port = rest[1:]
    else:
        host, _, port = authority.partition(":")
    if port and not (port.isascii() and port.isdigit()):
        return None, -1
    number = int(port) if port else None
    return _normalize_host(host), None if number in (None, default_port) else number


@functools.lru_cache(maxsize=4096)
def _normalize_host(host: str) -> str | None:
    """Return a host as Pydantic normalizes `BASE_URL`'s (WHATWG URL host parsing), or None if it isn't a valid one."""
    if not host:
        return None
    try:
        return Url(f"http://{host}/").host
    except ValidationError:
        return None


def _to_uri(text: str, safe: str) -> str:
    """Return part of an IRI in URI form: control, non-ASCII and other characters not in `safe` percent-encoded."""
    if safe is _PATH_SAFE and _PATH_AS_IS.fullmatch(text):  # most paths, quickly
        return text
    return urllib.parse.quote(text, safe=safe)
