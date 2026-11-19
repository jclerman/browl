"""Page tabs (DESIGN.md §2.9, U-28): which tab a request asks for, and each tab's links.

The first tab's URL is the page's own, without `tab`.
"""

import urllib.parse
from dataclasses import dataclass

from fastapi import Request

TAB_PARAM = "tab"
"""The query parameter that names a page's tab."""

PANEL_PARAM = "panel"
"""The query parameter that asks for the tab panel alone, as `panel=1`."""


@dataclass(frozen=True)
class Tab:
    """A page tab.

    Attributes:
        slug: Its name in URLs, as `?tab={slug}`.
        label: Its text.
    """

    slug: str
    label: str


@dataclass(frozen=True)
class TabQuery:
    """What a request's query asks of a page's tabs.

    Attributes:
        tab: The `tab` parameter; None if absent.
        panel: Whether it asks for the tab panel alone (`panel=1`).
    """

    tab: str | None
    panel: bool


@dataclass(frozen=True)
class TabLink:
    """A tab, as a page links it.

    Attributes:
        tab: The tab.
        href: Its URL, root-relative, with the page's fragment if it has one.
        panel: The URL of its panel alone, root-relative.
        selected: Whether it's the tab the page shows.
    """

    tab: Tab
    href: str
    panel: str
    selected: bool


def read_tab_query(request: Request) -> TabQuery | None:
    """Return what a request's query asks of a page's tabs; None if it repeats `tab` or `panel`, or `panel` isn't 1."""
    params = request.query_params
    tabs = params.getlist(TAB_PARAM)
    panels = params.getlist(PANEL_PARAM)
    if len(tabs) > 1 or len(panels) > 1 or panels not in ([], ["1"]):
        return None
    return TabQuery(tabs[0] if tabs else None, bool(panels))


class TabNotFoundError(LookupError):
    """A request asked for a tab, or a panel alone, that the page lacks, so it gets a 404."""


def choose_tab(tabs: tuple[Tab, ...], query: TabQuery, lenient: bool) -> Tab | None:
    """Return the tab a query asks for, else the page's first; None if the page has no tabs.

    Args:
        tabs: The page's tabs; empty if it has none, when `tab` is ignored, since a link may carry it on to such a page
            (e.g. a hash URL whose IRI has an `http` and an `https` form).
        query: What the request asks.
        lenient: Whether to show the first tab of a page that lacks the asked-for one, rather than refuse it, unless
            the request is for the panel alone. A page with the hash-IRI script is lenient: its query is also its
            fragment's entity's, which may have tabs this page lacks.

    Raises:
        TabNotFoundError: The page has tabs but not this one, or has none and the request is for a panel alone.
    """
    if not tabs:
        if query.panel:
            raise TabNotFoundError
        return None
    if query.tab is None:
        return tabs[0]
    tab = next((tab for tab in tabs if tab.slug == query.tab), None)
    if tab is not None:
        return tab
    if lenient and not query.panel:
        return tabs[0]
    raise TabNotFoundError


def with_query(url: str, params: list[tuple[str, str]]) -> str:
    """Return a URL, which may have a query, with parameters added to its query."""
    if not params:
        return url
    return f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"


def tab_links(
    tabs: tuple[Tab, ...],
    selected: Tab,
    page: str,
    fragment: str | None,
    panel: str,
) -> list[TabLink]:
    """Return a page's tab links.

    Args:
        tabs: The page's tabs.
        selected: The tab it shows.
        page: The page's URL, root-relative, without `tab` or a fragment.
        fragment: The page's fragment, in URI form, if it has one.
        panel: The URL its panels are fetched from, root-relative, without `tab` or `panel`.
    """
    links = []
    for tab in tabs:
        params = [] if tab == tabs[0] else [(TAB_PARAM, tab.slug)]
        href = with_query(page, params)
        if fragment is not None:
            href = f"{href}#{fragment}"
        panel_href = with_query(panel, [*params, (PANEL_PARAM, "1")])
        links.append(TabLink(tab, href, panel_href, tab == selected))
    return links
