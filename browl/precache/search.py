"""The search pre-caching hook (P-19, P-42): indexes the name-like annotation values with the search engine in use."""

from browl.precache.core import PreCacheContext, hook
from browl.search import BACKEND


@hook("language_ranking")
async def search(context: PreCacheContext) -> None:
    """Index the values the annotation values hook stored (`browl.search.SearchBackend.index`)."""
    await BACKEND.index(context)
