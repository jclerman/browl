"""Tests for search (P-19): the search hook's index and `SqliteSearch`'s ranking, filters and paging."""

from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import pytest

from browl.derived import DerivedReader, EntityType
from browl.precache.core import Hook
from browl.precache.entities import entities
from browl.precache.keys import build_keys
from browl.precache.names import names
from browl.precache.search import search as search_hook
from browl.precache.stage import pre_caching_stage
from browl.search import SearchQuery, SearchResults, SqliteSearch, search
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
    """Check that a shorter value ranks first (rule 4), by the normalized value's length."""
    settings = make_settings()
    await build(settings, [("longer", LABEL, LABEL, '"abcd"@en'), ("shorter", LABEL, LABEL, '"abc"@en')])
    assert found(settings, "ab") == ["shorter", "longer"]


async def test_alphabetical(data_dir: Path) -> None:
    """Check that values of a length rank by normalized value, then by value (rule 5), across entity types."""
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
    await build(settings, [("x", LABEL, LABEL, '"red blood cell"@en')])
    assert found(settings, "blood") == []
    assert found(settings, "  red b") == ["x"]
    assert found(settings, "red blood cells") == []


async def test_nothing_to_find(data_dir: Path) -> None:
    """Check that a blank query, or a blank value, finds nothing."""
    settings = make_settings()
    await build(settings, [("x", LABEL, LABEL, '"  "@en'), ("y", LABEL, LABEL, '"y"@en')])
    assert find(settings, "") == SearchResults([], more=False)
    assert find(settings, " \t") == SearchResults([], more=False)
    assert found(settings, " ") == []


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
    assert [hit.types for hit in find(settings, "tr", types={EntityType.ANNOTATION_PROPERTY}).hits] == [
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
    assert found(settings, "k", types={EntityType.DATA_PROPERTY}) == ["property"]
    assert found(settings, "k", properties={f"{SKOS}prefLabel"}) == ["pref"]
    assert found(settings, "k", entailed=False) == ["class", "property", "pref"]
    assert found(settings, "k", types=set()) == []


async def test_paging(data_dir: Path) -> None:
    """Check that the offset and limit page through the results, and that `more` says whether any follow."""
    settings = make_settings()
    await build(settings, [(f"e{n}", LABEL, LABEL, f'"m{n}"@en') for n in range(5)])
    assert find(settings, "m", limit=2) == find(settings, "m", limit=2, offset=0)
    pages = [find(settings, "m", limit=2, offset=offset) for offset in (0, 2, 4)]
    assert [[hit.value for hit in page.hits] for page in pages] == [["m0", "m1"], ["m2", "m3"], ["m4"]]
    assert [page.more for page in pages] == [True, True, False]
    assert find(settings, "m", limit=0) == SearchResults([], more=False)


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
