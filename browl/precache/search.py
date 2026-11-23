"""The search pre-caching hook (P-19): indexes the name values with the search engine in use (DESIGN.md §2.6)."""

from browl.precache.core import PreCacheContext, hook
from browl.search import BACKEND


@hook("name_properties", "language_ranking", "extra_prefixes")
async def search(context: PreCacheContext) -> None:
    """Index the name values that the names hook stored (`browl.search.SearchBackend.index`)."""
    await BACKEND.index(context)
