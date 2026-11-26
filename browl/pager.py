"""The results pager (DESIGN.md §2.8, §2.9): Previous, Next, and a form to go to any page, shared by paged lists.

A list paged by `page=…`, from 1 and absent for the first, has a `Pager` (`pager`), which `templates/pager.html`
renders. A pager with a panel URL swaps pages into a tab panel with htmx (`static/tabs.js`), as the Usage tab does;
without one, its links and form load whole pages, as the search results page does. Both work without JS.
"""

import math
import re
import urllib.parse
from dataclasses import dataclass

from fastapi import Request

from browl.pages import href
from browl.tabs import PANEL_PARAM, with_query

PAGE_PARAM = "page"
"""The query parameter that names a page of a paged list, from 1; absent for the first."""

_PAGE_NUMBER = re.compile(r"[1-9][0-9]{0,8}")
"""A page number `page=…` may give: a positive integer, without a sign or leading zeros, so each page has one URL."""


@dataclass(frozen=True)
class PageLink:
    """A pager's link to another page.

    Attributes:
        href: The page's URL, root-relative.
        panel: The URL its panel is fetched from, root-relative; None if the pager loads whole pages.
    """

    href: str
    panel: str | None


@dataclass(frozen=True)
class Pager:
    """A paged list's links to other pages, and its form to go to a page.

    Attributes:
        name: What the pages are of, for the navigation's label ("Usage pages") and its controls' `id`s, which keep the
            focus when htmx swaps a page in.
        page: The page shown, from 1.
        pages: How many pages can be reached.
        more: Whether there are more results past the last page that can be reached, so it's "of 21+".
        previous: The link to the previous page; None on the first.
        next: The link to the next page; None on the last.
        action: The form's URL, root-relative, without its query, which the form's fields replace.
        fields: The form's hidden fields: the page URL's own query, and the list's parameters.
        panel: This page's panel URL, root-relative, which `tabs.js` keeps in the history; None if the pager loads
            whole pages.
        action_panel: The URL htmx sends the form's fields to for a page's panel, root-relative; None as `panel`.
        nofollow: Whether search engines are asked not to follow its links.
    """

    name: str
    page: int
    pages: int
    more: bool
    previous: PageLink | None
    next: PageLink | None
    action: str
    fields: list[tuple[str, str]]
    panel: str | None = None
    action_panel: str | None = None
    nofollow: bool = False

    @property
    def id(self) -> str:
        """Return the prefix of its controls' `id`s."""
        return self.name.lower().replace(" ", "-")


def page_params(page: int) -> list[tuple[str, str]]:
    """Return the query parameters that name a page: none for the first.

    Examples:
        >>> page_params(1), page_params(3)
        ([], [('page', '3')])
    """
    return [(PAGE_PARAM, str(page))] if page > 1 else []


def read_page(request: Request) -> int | None:
    """Return the page a request asks for, 1 if none; None if it repeats `page=` or isn't a page number."""
    values = request.query_params.getlist(PAGE_PARAM)
    if not values:
        return 1
    if len(values) > 1 or not _PAGE_NUMBER.fullmatch(values[0]):
        return None
    return int(values[0])


def page_count(count: int, size: int) -> int:
    """Return how many pages a count fills, at least one.

    Examples:
        >>> page_count(0, 50), page_count(50, 50), page_count(51, 50)
        (1, 1, 2)
    """
    return max(math.ceil(count / size), 1)


def make_pager(
    request: Request,
    name: str,
    page: int,
    pages: int,
    url: str,
    params: list[tuple[str, str]],
    fragment: str | None = None,
    panel: str | None = None,
    more: bool = False,
    nofollow: bool = False,
) -> Pager | None:
    """Return a paged list's pager, if it has more than one page.

    Args:
        request: The request.
        name: What the pages are of (`Pager.name`).
        page: The page shown.
        pages: How many pages can be reached.
        url: The page's URL, from the app's root, without the list's parameters or a fragment; it may have a query.
        params: The list's parameters, which each page's URL adds, before `page=`.
        fragment: The page's fragment, in URI form, if it has one.
        panel: The URL its pages' panels are fetched from, from the app's root, without the list's parameters or
            `panel=`; None for a pager that loads whole pages. The page's URL, or a URL of its own, such as a hash
            route's, which keeps its query when the form sends its fields.
        more: Whether there are more results past the last page that can be reached.
        nofollow: Whether search engines are asked not to follow its links.
    """
    if pages < 2:
        return None

    def panel_href(number: int) -> str | None:
        if panel is None:
            return None
        return href(request, with_query(panel, [*params, *page_params(number), (PANEL_PARAM, "1")]))

    def link(number: int) -> PageLink | None:
        if not 1 <= number <= pages:
            return None
        return PageLink(href(request, with_query(url, [*params, *page_params(number)]), fragment), panel_href(number))

    path, _, query = url.partition("?")
    action_panel = None
    if panel is not None:
        # The page's own query is in the fields, a panel URL of its own's isn't.
        action_panel = href(request, with_query(path if panel == url else panel, [(PANEL_PARAM, "1")]))
    return Pager(
        name=name,
        page=page,
        pages=pages,
        more=more,
        previous=link(page - 1),
        next=link(page + 1),
        action=href(request, path, fragment),
        fields=[*urllib.parse.parse_qsl(query, keep_blank_values=True), *params],
        panel=panel_href(page),
        action_panel=action_panel,
        nofollow=nofollow,
    )
