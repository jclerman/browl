"""Tests for search (P-19): the search hook's index and `SqliteSearch`'s ranking, filters and paging."""

from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import pytest
from pydantic import ValidationError

from browl.derived import DerivedReader, EntityType
from browl.precache.core import Hook
from browl.precache.entities import entities
from browl.precache.keys import build_keys
from browl.precache.names import names
from browl.precache.search import search as search_hook
from browl.precache.stage import HOOKS, pre_caching_stage
from browl.search import (
    KEY_CHARS,
    MAX_LIMIT,
    MAX_OFFSET,
    MAX_QUERY_CHARS,
    SearchQuery,
    SearchResults,
    SqliteSearch,
    search,
)
from browl.settings import Settings
from tests.helpers import LABEL, ONTOLOGY_RESULT, connect, fuseki, job, make_settings
from tests.test_precache_names import PREF_LABEL, SKOS, entity_result, name_result

pytestmark = pytest.mark.anyio

EX = "http://example.org/"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
SUB = f"<{EX}sub>"  # a sub-property of a name property: its values are entailed
HOOKS_USED: Sequence[Hook] = (entities, names, search_hook)

type Value = tuple[str, str, str, str]  # entity's local name, name property, property, literal


async def build(settings: Settings, values: Sequence[Value], declared: Sequence[tuple[str, str]] = ()) -> None:
    """Build generation 1's derived database, declaring the entities given (local name, OWL type), else classes.

    Without declared entities, each entity named in the values is a class.
    """
    kinds = declared or [(name, "Class") for name in dict.fromkeys(name for name, *_ in values)]
    rows = [(f"{EX}{name}", n, p, literal) for name, n, p, literal in values]
    found = [(f"{EX}{name}", kind) for name, kind in kinds]
    async with fuseki(ONTOLOGY_RESULT, entity_result(*found), name_result(*rows)) as http:
        await pre_caching_stage(job(settings, 1), http, hooks=HOOKS_USED)


def find(settings: Settings, text: str, **filters: object) -> SearchResults:
    """Return the results of a query on generation 1."""
    with closing(connect(settings, 1, HOOKS_USED)) as connection:
        return search(connection, SearchQuery(text, **filters))  # type: ignore[arg-type]


def found(settings: Settings, text: str, **filters: object) -> list[str]:
    """Return the local names of the IRIs a query finds, best first."""
    return [hit.iri.removeprefix(EX) for hit in find(settings, text, **filters).hits]


def seeks(settings: Settings, text: str, **filters: object) -> int:
    """Return how many reads of `search_value` a query makes."""
    statements: list[str] = []
    with closing(connect(settings, 1, HOOKS_USED)) as connection:
        connection.set_trace_callback(statements.append)
        search(connection, SearchQuery(text, **filters))  # type: ignore[arg-type]
    return sum("FROM search_value" in statement for statement in statements)


def indexed(settings: Settings) -> list[tuple[object, ...]]:
    """Return each `search_value` row's entity's local name, length, and whole normalized value and value."""
    with closing(connect(settings, 1, HOOKS_USED)) as connection:
        rows = connection.execute(
            "SELECT iri, length, normalized || coalesce(normalized_rest, ''),"
            " value || coalesce(value_rest, ''), language"
            " FROM search_value JOIN entity ON entity.id = search_value.entity ORDER BY iri"
        )
        return [(str(iri).removeprefix(EX), *rest) for iri, *rest in rows]


async def test_exact_match_first(data_dir: Path) -> None:
    """Check that an exact match ranks first, even under a later name property and a worse language (rule 1)."""
    settings = make_settings(NAME_PROPERTIES=["rdfs:label", "skos:prefLabel"])
    await build(settings, [("prefix", LABEL, LABEL, '"bones"@en'), ("exact", PREF_LABEL, PREF_LABEL, '"Bone"@de')])
    results = find(settings, "bone")
    assert [(hit.iri, hit.exact) for hit in results.hits] == [(f"{EX}exact", True), (f"{EX}prefix", False)]


async def test_property_position(data_dir: Path) -> None:
    """Check that a value of an earlier name property ranks first, even if it's longer (rule 2)."""
    settings = make_settings(NAME_PROPERTIES=["skos:prefLabel", "rdfs:label"])
    await build(settings, [("label", LABEL, LABEL, '"cat"@en'), ("pref", PREF_LABEL, PREF_LABEL, '"catalogue"@en')])
    assert found(settings, "ca") == ["pref", "label"]


async def test_language_position(data_dir: Path) -> None:
    """Check that values rank by language: listed first, then unlisted, then untagged (rule 3, O-5)."""
    settings = make_settings(LANGUAGE_RANKING=["fr", "en"])
    values = [
        ("untagged", LABEL, LABEL, '"do"'),
        ("unlisted", LABEL, LABEL, '"dog"@de'),
        ("en", LABEL, LABEL, '"dogs"@en-GB'),  # falls back to en
        ("fr", LABEL, LABEL, '"doggies"@fr'),
    ]
    await build(settings, values)
    assert found(settings, "d") == ["fr", "en", "unlisted", "untagged"]
    assert [hit.language for hit in find(settings, "d").hits] == ["fr", "en-gb", "de", None]


async def test_unlisted_languages_tie(data_dir: Path) -> None:
    """Check that unlisted languages rank alike, so length decides between them."""
    settings = make_settings(LANGUAGE_RANKING=["en"])
    await build(settings, [("de", LABEL, LABEL, '"hunde"@de'), ("it", LABEL, LABEL, '"hu"@it')])
    assert found(settings, "h") == ["it", "de"]


async def test_direct_first(data_dir: Path) -> None:
    """Check that a direct value ranks before one entailed through a sub-property, even if it's longer."""
    settings = make_settings()
    await build(settings, [("entailed", LABEL, SUB, '"fish"@en'), ("direct", LABEL, LABEL, '"fishes"@en')])
    assert found(settings, "fi") == ["direct", "entailed"]


async def test_shorter_first(data_dir: Path) -> None:
    """Check that a shorter value ranks first (rule 5), by the normalized value's length."""
    settings = make_settings()
    await build(settings, [("longer", LABEL, LABEL, '"abcd"@en'), ("shorter", LABEL, LABEL, '"abc"@en')])
    assert found(settings, "ab") == ["shorter", "longer"]


async def test_alphabetical(data_dir: Path) -> None:
    """Check that values of a length rank by normalized value, then by value (rule 6), across entity types."""
    settings = make_settings()
    values = [
        ("anu", LABEL, LABEL, '"anu"@en'),
        ("lower", LABEL, LABEL, '"ant"@en'),
        ("upper", LABEL, LABEL, '"Ant"@en'),
        ("accent", LABEL, LABEL, '"Âns"@en'),
        ("property", LABEL, LABEL, '"anz"@en'),
    ]
    declared = [(name, "Class") for name in ("anu", "lower", "upper", "accent")] + [("property", "ObjectProperty")]
    await build(settings, values, declared)
    assert found(settings, "an") == ["accent", "upper", "lower", "anu", "property"]


async def test_alphabetical_across_lanes(data_dir: Path) -> None:
    """Check that equal normalized values rank by value across lanes, not by their lanes' order."""
    settings = make_settings()
    values = [("lower", LABEL, LABEL, '"ant"@en'), ("upper", LABEL, LABEL, '"Ant"@en')]
    await build(settings, values, [("lower", "Class"), ("upper", "ObjectProperty")])
    assert found(settings, "an") == ["upper", "lower"]


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([("label", LABEL, LABEL, '"cat"@de'), ("pref", PREF_LABEL, PREF_LABEL, '"ca"@en')], ["label", "pref"]),
        ([("direct", LABEL, LABEL, '"cat"@de'), ("entailed", LABEL, SUB, '"ca"@en')], ["entailed", "direct"]),
        ([("pref", PREF_LABEL, PREF_LABEL, '"ca"@en'), ("entailed", LABEL, SUB, '"cat"@en')], ["entailed", "pref"]),
    ],
    ids=["property before language", "language before direct", "property before direct"],
)
async def test_rule_order(data_dir: Path, values: list[Value], expected: list[str]) -> None:
    """Check that the rules after exact match apply in order: property, then language, then direct (rules 2-4)."""
    settings = make_settings(NAME_PROPERTIES=["rdfs:label", "skos:prefLabel"], LANGUAGE_RANKING=["en"])
    await build(settings, values)
    assert found(settings, "c") == expected


async def test_exact_matches_in_tier_order(data_dir: Path) -> None:
    """Check that exact matches rank among themselves by the later rules, here the property's position."""
    settings = make_settings(NAME_PROPERTIES=["skos:prefLabel", "rdfs:label"])
    await build(settings, [("label", LABEL, LABEL, '"bone"@en'), ("pref", PREF_LABEL, PREF_LABEL, '"Bone"@en')])
    assert found(settings, "bone") == ["pref", "label"]


async def test_best_value_found_later(data_dir: Path) -> None:
    """Check that an IRI's best value is its exact match, even under a later property than a prefix match."""
    settings = make_settings(NAME_PROPERTIES=["rdfs:label", "skos:prefLabel"])
    await build(settings, [("x", LABEL, LABEL, '"bones"@en'), ("x", PREF_LABEL, PREF_LABEL, '"bone"@en')])
    assert [(hit.value, hit.exact) for hit in find(settings, "bone").hits] == [("bone", True)]


@pytest.mark.parametrize(
    ("ranking", "tags"), [(["en"], ("en-GB", "en-US")), (["en"], ("de", "fr"))], ids=["fallback", "unlisted"]
)
async def test_same_value_languages_alike(data_dir: Path, ranking: list[str], tags: tuple[str, str]) -> None:
    """Check that one value in two languages that rank alike is indexed once, under the first tag."""
    settings = make_settings(LANGUAGE_RANKING=ranking)
    await build(settings, [("x", LABEL, LABEL, f'"Bone"@{tag}') for tag in tags])
    assert [(hit.value, hit.language) for hit in find(settings, "bone").hits] == [("Bone", tags[0].lower())]


async def test_long_values(data_dir: Path) -> None:
    """Check values longer than the key: found by prefix, and by a query longer than the key, exactly or not."""
    settings = make_settings()
    long = "a" * KEY_CHARS
    values = [
        ("x", LABEL, LABEL, f'"{long}x"@en'),
        ("xy", LABEL, LABEL, f'"{long}xy"@en'),
        ("b", LABEL, LABEL, '"ab"@en'),
    ]
    await build(settings, values)
    assert [length for _, length, *_ in indexed(settings)] == [2, KEY_CHARS, KEY_CHARS]
    assert found(settings, "a") == ["b", "x", "xy"]
    assert [(hit.iri.removeprefix(EX), hit.exact) for hit in find(settings, f"{long}x").hits] == [
        ("x", True),
        ("xy", False),
    ]
    assert found(settings, f"{long}xy") == ["xy"]
    assert found(settings, f"{long}z") == []
    assert [hit.value for hit in find(settings, f"{long}xy").hits] == [f"{long}xy"]


async def test_long_values_bound_the_walk(data_dir: Path) -> None:
    """Check that a value far longer than the key doesn't make a query seek more than the key's length."""
    settings = make_settings()
    await build(settings, [("long", LABEL, LABEL, f'"b{"x" * 10_000}"@en'), ("short", LABEL, LABEL, '"ab"@en')])
    assert found(settings, "a") == ["short"]
    assert seeks(settings, "a") <= KEY_CHARS


async def test_nul_counted(data_dir: Path) -> None:
    """Check that a value's length counts the characters after a NUL, which SQLite's length() wouldn't."""
    settings = make_settings()
    await build(settings, [("nul", LABEL, LABEL, '"ab\\u0000cd"@en'), ("abz", LABEL, LABEL, '"abz"@en')])
    assert [(hit.iri.removeprefix(EX), hit.exact) for hit in find(settings, "ab").hits] == [
        ("abz", False),
        ("nul", False),
    ]
    assert found(settings, "ab\x00c") == ["nul"]


@pytest.mark.parametrize(
    ("value", "query"),
    [("École", "ecole"), ("ecole", "ÉCOLE"), ("STRASSE", "straße"), ("ﬁsh", "fi")],
    ids=["accent in value", "accent in query", "case folding", "compatibility"],
)
async def test_case_and_accents(data_dir: Path, value: str, query: str) -> None:
    """Check that matching ignores case and accents (U-11)."""
    settings = make_settings()
    await build(settings, [("x", LABEL, LABEL, f'"{value}"@en')])
    assert [hit.value for hit in find(settings, query).hits] == [value]


async def test_matches_start_of_value(data_dir: Path) -> None:
    """Check that a query matches only the start of a whole value, and that leading whitespace is ignored."""
    settings = make_settings()
    await build(settings, [("x", LABEL, LABEL, '"red blood cell"@en'), ("y", LABEL, LABEL, '" \\nHomo sapiens"@en')])
    assert found(settings, "blood") == []
    assert found(settings, "  red b") == ["x"]
    assert found(settings, "red blood cells") == []
    assert [(hit.value, hit.exact) for hit in find(settings, " homo sapiens").hits] == [(" \nHomo sapiens", True)]


async def test_nothing_to_find(data_dir: Path) -> None:
    """Check that a blank query finds nothing, and that a value that normalizes to nothing isn't indexed.

    Such a value (only whitespace, or combining marks) matches no query, and would give its lane a longest value of 0.
    """
    settings = make_settings()
    values = [("x", LABEL, LABEL, '"  "@en'), ("y", LABEL, LABEL, '"y"@en'), ("z", LABEL, SUB, '"\u0301"@en')]
    await build(settings, values)
    assert [name for name, *_ in indexed(settings)] == ["y"]
    assert find(settings, "") == SearchResults((), more=False)
    assert find(settings, " \t") == SearchResults((), more=False)
    assert found(settings, "y") == ["y"]


async def test_grouped_per_iri(data_dir: Path) -> None:
    """Check that an IRI is one hit with its best value, and that a punned IRI is one hit listing its types."""
    settings = make_settings(NAME_PROPERTIES=["rdfs:label", "skos:prefLabel"])
    values = [
        ("x", PREF_LABEL, PREF_LABEL, '"tree"@en'),
        ("x", LABEL, LABEL, '"treelike"@en'),
        ("x", LABEL, LABEL, '"trees"@en'),
        ("p", LABEL, LABEL, '"trunk"@en'),
    ]
    await build(settings, values, [("x", "Class"), ("p", "Class"), ("p", "AnnotationProperty")])
    results = find(settings, "tr")
    assert [(hit.iri, hit.value, hit.property, hit.types) for hit in results.hits] == [
        (f"{EX}x", "trees", RDFS_LABEL, (EntityType.CLASS,)),
        (f"{EX}p", "trunk", RDFS_LABEL, (EntityType.CLASS, EntityType.ANNOTATION_PROPERTY)),
    ]
    assert results.more is False
    assert [hit.types for hit in find(settings, "tr", types=frozenset({EntityType.ANNOTATION_PROPERTY})).hits] == [
        (EntityType.ANNOTATION_PROPERTY,)
    ]


async def test_filters(data_dir: Path) -> None:
    """Check the filters by entity type, name property, and direct values only (U-10)."""
    settings = make_settings(NAME_PROPERTIES=["rdfs:label", "skos:prefLabel"])
    values = [
        ("class", LABEL, LABEL, '"ka"@en'),
        ("property", LABEL, LABEL, '"kb"@en'),
        ("pref", PREF_LABEL, PREF_LABEL, '"kc"@en'),
        ("entailed", LABEL, SUB, '"kd"@en'),
    ]
    declared = [("class", "Class"), ("property", "DatatypeProperty"), ("pref", "Class"), ("entailed", "Class")]
    await build(settings, values, declared)
    assert found(settings, "k") == ["class", "property", "entailed", "pref"]
    assert found(settings, "k", types=frozenset({EntityType.DATA_PROPERTY})) == ["property"]
    assert found(settings, "k", properties=frozenset({f"{SKOS}prefLabel"})) == ["pref"]
    assert found(settings, "k", entailed=False) == ["class", "property", "pref"]
    assert found(settings, "k", types=frozenset({EntityType.CLASS}), entailed=False) == ["class", "pref"]
    assert found(settings, "k", types=frozenset({EntityType.DATA_PROPERTY}), properties=frozenset({RDFS_LABEL})) == [
        "property"
    ]
    assert found(settings, "k", types=frozenset()) == []
    assert found(settings, "k", properties=frozenset()) == []


async def test_paging(data_dir: Path) -> None:
    """Check that the offset and limit page through the results, and that `more` says whether any follow."""
    settings = make_settings()
    await build(settings, [(f"e{n}", LABEL, LABEL, f'"m{n}"@en') for n in range(5)])
    assert find(settings, "m", limit=2) == find(settings, "m", limit=2, offset=0)
    pages = [find(settings, "m", limit=2, offset=offset) for offset in (0, 2, 4)]
    assert [[hit.value for hit in page.hits] for page in pages] == [["m0", "m1"], ["m2", "m3"], ["m4"]]
    assert [page.more for page in pages] == [True, True, False]
    assert find(settings, "m", limit=1, offset=4) == find(settings, "m", limit=2, offset=4)  # ends at the last
    assert find(settings, "m", limit=1, offset=4).more is False
    assert find(settings, "m", offset=5) == SearchResults((), more=False)


async def test_paging_across_exact_matches(data_dir: Path) -> None:
    """Check paging from the exact matches into the prefix matches.

    Exact matches enough for the page, and one more, end the search before the prefix matches.
    """
    settings = make_settings()
    values = [("lower", LABEL, LABEL, '"m"@en'), ("upper", LABEL, LABEL, '"M"@en'), ("prefix", LABEL, LABEL, '"mm"@en')]
    await build(settings, values)
    first, second = (find(settings, "m", limit=2, offset=offset) for offset in (0, 2))
    assert [(hit.value, hit.exact) for hit in first.hits] == [("M", True), ("m", True)]
    assert first.more is True
    assert [(hit.value, hit.exact) for hit in second.hits] == [("mm", False)]
    assert second.more is False
    assert seeks(settings, "m", limit=1) < seeks(settings, "m", limit=2)


def test_search_hook_after_names() -> None:
    """Check that the search hook runs after the names hook, whose `name_value` it reads."""
    assert HOOKS.index(search_hook) > HOOKS.index(names)


async def test_many_lengths(data_dir: Path) -> None:
    """Check that the search reaches long values past many lengths with no match, and stops once it has enough."""
    settings = make_settings()
    await build(settings, [("long", LABEL, LABEL, f'"q{"x" * 200}"@en'), ("short", LABEL, LABEL, '"qq"@en')])
    assert found(settings, "q") == ["short", "long"]
    assert found(settings, "q", limit=1) == ["short"]


async def test_backend(data_dir: Path) -> None:
    """Check that `SqliteSearch` reads the generation's derived database in the thread pool."""
    settings = make_settings()
    await build(settings, [("x", LABEL, LABEL, '"apple"@en')])
    reader = DerivedReader(settings.data_dir, build_keys(settings, HOOKS_USED))
    try:
        results = await SqliteSearch(reader).search(1, SearchQuery("APP"))
    finally:
        reader.close()
    assert [hit.value for hit in results.hits] == ["apple"]


@pytest.mark.parametrize(
    "fields",
    [
        {"limit": 0},
        {"limit": MAX_LIMIT + 1},
        {"offset": -1},
        {"offset": MAX_OFFSET + 1},
        {"text": "x" * (MAX_QUERY_CHARS + 1)},
        {"text": "a\ud800"},
    ],
    ids=["no results", "too many results", "negative offset", "offset too deep", "text too long", "lone surrogate"],
)
def test_query_checked(fields: dict[str, object]) -> None:
    """Check the bounds of a query's fields, checked when it's made."""
    with pytest.raises(ValidationError):
        SearchQuery(**{"text": "x"} | fields)  # type: ignore[arg-type]


def test_query_filters_frozen() -> None:
    """Check that a query's filters are frozensets, whatever collection they're given as."""
    query = SearchQuery("x", types=[EntityType.CLASS], properties=(RDFS_LABEL,))
    assert (query.types, query.properties) == (frozenset({EntityType.CLASS}), frozenset({RDFS_LABEL}))
