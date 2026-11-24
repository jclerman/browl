"""Tests for search (P-19): the search hook's index and `SqliteSearch`'s ranking, filters and paging."""

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import pytest
from pydantic import ValidationError

from browl import search as search_module
from browl.derived import SCHEMA, DerivedReader, EntityType
from browl.names import name_property_iris
from browl.precache.core import Hook
from browl.precache.entities import entities
from browl.precache.keys import build_keys
from browl.precache.names import names
from browl.precache.search import search as search_hook
from browl.precache.stage import HOOKS, pre_caching_stage
from browl.precache.values import annotation_values
from browl.search import (
    KEY_CHARS,
    MAX_LIMIT,
    MAX_OFFSET,
    MAX_QUERY_CHARS,
    SearchQuery,
    SearchResults,
    SqliteSearch,
    search,
    search_properties,
    word_starts,
)
from browl.settings import Settings
from tests.helpers import (
    LABEL,
    ONTOLOGY_RESULT,
    SUB_END,
    SUB_HEADER,
    VALUES_END,
    VALUES_HEADER,
    connect,
    fuseki,
    job,
    make_settings,
)
from tests.test_precache_names import PREF_LABEL, SKOS, entity_result, name_result

pytestmark = pytest.mark.anyio

EX = "http://example.org/"
RDFS_LABEL = "http://www.w3.org/2000/01/rdf-schema#label"
SUB = f"<{EX}sub>"  # a sub-property of a name property: its values are entailed
HOOKS_USED: Sequence[Hook] = (entities, names, annotation_values, search_hook)

type Value = tuple[str, str, str, str]  # entity's local name, name property, property, literal


def values_result(*values: tuple[str, str, str]) -> str:
    """Return the annotation values query's result for (entity IRI, property, literal) rows."""
    return VALUES_HEADER + "".join(f"<{e}>\t{p}\t{v}\t\n" for e, p, v in values) + VALUES_END


def sub_result(*pairs: tuple[str, str]) -> str:
    """Return the sub-properties query's result for (sub-property, property) rows."""
    return SUB_HEADER + "".join(f"{sub}\t{prop}\t\n" for sub, prop in pairs) + SUB_END


async def build(settings: Settings, values: Sequence[Value], declared: Sequence[tuple[str, str]] = ()) -> None:
    """Build generation 1's derived database, declaring the entities given (local name, OWL type), else classes.

    Without declared entities, each entity named in the values is a class. A value's property is a sub-property of its
    name property, if they differ.
    """
    kinds = declared or [(name, "Class") for name in dict.fromkeys(name for name, *_ in values)]
    rows = [(f"{EX}{name}", n, p, literal) for name, n, p, literal in values]
    found = [(f"{EX}{name}", kind) for name, kind in kinds]
    asserted = dict.fromkeys((entity, p, literal) for entity, _, p, literal in rows)
    subs = dict.fromkeys((p, n) for _, n, p, _ in rows if p != n)
    bodies = (name_result(*rows), values_result(*asserted), sub_result(*subs))
    async with fuseki(ONTOLOGY_RESULT, entity_result(*found), *bodies) as http:
        await pre_caching_stage(job(settings, 1), http, hooks=HOOKS_USED)


def query(settings: Settings, text: str, **filters: object) -> SearchQuery:
    """Return a query, walking the name properties first, in order, as the search page's do."""
    return SearchQuery(text, **({"order": tuple(name_property_iris(settings))} | filters))  # type: ignore[arg-type]


def find(settings: Settings, text: str, **filters: object) -> SearchResults:
    """Return the results of a query on generation 1."""
    with closing(connect(settings, 1, HOOKS_USED)) as connection:
        return search(connection, query(settings, text, **filters))


def found(settings: Settings, text: str, **filters: object) -> list[str]:
    """Return the local names of the IRIs a query finds, best first."""
    return [hit.iri.removeprefix(EX) for hit in find(settings, text, **filters).hits]


def seeks(settings: Settings, text: str, **filters: object) -> int:
    """Return how many reads of `search_value` a query makes."""
    statements: list[str] = []
    with closing(connect(settings, 1, HOOKS_USED)) as connection:
        connection.set_trace_callback(statements.append)
        search(connection, query(settings, text, **filters))
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


@pytest.mark.parametrize(
    ("normalized", "starts"),
    [
        ("t-cell", [2]),
        ("1,3-dimethylbutane", [2, 4]),
        ("red blood cell", [4, 10]),
        ("h2o", []),  # a change between letters and digits isn't a word start
        ("abc123 def", [7]),
        ("cafe-creme", [5]),
        ("\u03b1-actin", [2]),  # non-Latin letters (here alpha) are alphanumeric
        ("белок клетки", [6]),
        ("細胞 核", [3]),
        ("(r)-lactate", [1, 4]),  # the value's own start isn't listed, but a word after punctuation there is
        ("a  b", [3]),  # a run of separators starts one word
        ("x--", []),
        ("_x", [1]),  # an underscore isn't alphanumeric
        ("", []),
    ],
)
def test_word_starts(normalized: str, starts: list[int]) -> None:
    """Check where a normalized value's later words start (§2.6, Any word)."""
    assert word_starts(normalized) == starts


@pytest.mark.parametrize(
    ("value", "query", "found"),
    [
        ("T-cell", "cell", True),
        ("1,3-dimethylbutane", "dimethyl", True),
        ("1,3-dimethylbutane", "3-dim", True),
        ("H2O", "2", False),
        ("H2O", "o", False),
        ("naïve T-cell", "cell", True),
        ("Ångström unit", "ngstrom", False),  # accents never split a word
        ("Ångström unit", "unit", True),
        ("Café crème", "creme", True),  # an accent in the word is ignored too
        ("\u03b1-Actin", "actin", True),
        ("белок Клетки", "клет", True),
        ("red blood cell", "blood cell", True),  # from a word on, across later words
        ("red blood cell", "lood", False),
        ("red blood cell", "  cell", True),  # the query's leading whitespace is ignored
    ],
)
async def test_word_match(data_dir: Path, value: str, query: str, found: bool) -> None:
    """Check that a query matches the start of a later word, and nowhere else inside a value (U-19)."""
    settings = make_settings()
    await build(settings, [("x", LABEL, LABEL, f'"{value}"@en')])
    assert [hit.value for hit in find(settings, query).hits] == ([value] if found else [])


async def test_whole_value_before_word(data_dir: Path) -> None:
    """Check that matches from a whole value's start rank before word matches (rule 2), which rank by the later rules.

    A whole value's match ranks first even under a later name property, in a worse language, or longer.
    """
    settings = make_settings(NAME_PROPERTIES=["rdfs:label", "skos:prefLabel"], LANGUAGE_RANKING=["en"])
    await build(
        settings,
        [
            ("word_long", LABEL, LABEL, '"B-cell lymphoma"@en'),
            ("word_exact", LABEL, LABEL, '"T-cell"@en'),
            ("word_pref", PREF_LABEL, PREF_LABEL, '"A-cel"@en'),
            ("whole", PREF_LABEL, PREF_LABEL, '"cellular component of something"@fr'),
        ],
    )
    results = find(settings, "cel")
    assert [(hit.iri.removeprefix(EX), hit.exact, hit.word_start) for hit in results.hits] == [
        ("whole", False, 0),
        ("word_exact", False, 2),  # rdfs:label, then shorter from the word on
        ("word_long", False, 2),
        ("word_pref", False, 2),  # skos:prefLabel
    ]


async def test_word_match_once_per_iri(data_dir: Path) -> None:
    """Check that an IRI whose values match from the start and from a word is one hit, with its best match."""
    settings = make_settings()
    await build(settings, [("x", LABEL, LABEL, '"cell"@en'), ("x", LABEL, LABEL, '"T-cell cell"@en')])
    results = find(settings, "cell")
    assert [(hit.value, hit.exact, hit.word_start) for hit in results.hits] == [("cell", True, 0)]
    assert not results.more


async def test_word_rows(data_dir: Path) -> None:
    """Check the index's rows of a value: the whole value's, then one per later word start, keyed from it."""
    settings = make_settings()
    await build(settings, [("x", LABEL, LABEL, '"Red  Blood-Cell"@en')])
    with closing(connect(settings, 1, HOOKS_USED)) as connection:
        rows = connection.execute(
            "SELECT word, length, normalized, value, word_start FROM search_value ORDER BY word, word_start"
        ).fetchall()
        lanes = connection.execute("SELECT word, max_length FROM search_lane ORDER BY word").fetchall()
    assert rows == [
        (0, 15, "red  blood-cell", "Red  Blood-Cell", 0),
        (1, 10, "blood-cell", "Red  Blood-Cell", 5),
        (1, 4, "cell", "Red  Blood-Cell", 11),
    ]
    assert lanes == [(0, 15), (1, 10)]


async def test_word_rows_bounded(data_dir: Path) -> None:
    """Check that a long value of many words stores a bounded amount in its word rows.

    Only words within its first `MAX_QUERY_CHARS` normalized characters get rows, each keyed by at most that many, with
    at most `KEY_CHARS` characters of the value.
    """
    settings = make_settings()
    value = " ".join(f"w{n:04}" for n in range(2_000))  # 11,999 characters; a word every 6
    await build(settings, [("x", LABEL, LABEL, f'"{value}"@en')])
    with closing(connect(settings, 1, HOOKS_USED)) as connection:
        rows, stored = connection.execute(
            "SELECT count(*), sum(length(normalized) + length(coalesce(normalized_rest, ''))"
            " + length(value) + length(coalesce(value_rest, ''))) FROM search_value WHERE word = 1"
        ).fetchone()
    assert rows == len(range(6, MAX_QUERY_CHARS, 6))
    assert stored <= rows * (MAX_QUERY_CHARS + KEY_CHARS)  # unbounded, about 18 M characters
    assert [hit.word_start for hit in find(settings, "w0100 w0101").hits] == [600]
    assert find(settings, "w0500").hits == ()  # at 3,000, past the bound


async def test_long_word_match(data_dir: Path) -> None:
    """Check that queries longer than `KEY_CHARS` match word rows, and that a hit has the whole value.

    The value's word rows share their first `KEY_CHARS` characters of key from many word starts on, and each is kept.
    """
    settings = make_settings()
    repeated = "x-" * 300 + "end"
    await build(settings, [("rep", LABEL, LABEL, f'"{repeated}"@en'), ("pre", LABEL, LABEL, f'"pre {"y" * 300}"@en')])
    assert [(hit.value, hit.word_start) for hit in find(settings, "x-" * 140 + "end").hits] == [(repeated, 320)]
    assert [(hit.value, hit.word_start) for hit in find(settings, "y" * 290).hits] == [(f"pre {'y' * 300}", 4)]


async def test_word_paging(data_dir: Path) -> None:
    """Check that paging runs on from whole-value matches into word matches, and `more` says when it ends."""
    settings = make_settings()
    whole = [(f"w{n}", LABEL, LABEL, f'"cel{n}"@en') for n in range(3)]
    words = [(f"x{n}", LABEL, LABEL, f'"a cel{n}"@en') for n in range(3)]
    await build(settings, whole + words)
    pages = [find(settings, "cel", limit=2, offset=offset) for offset in (0, 2, 4)]
    assert [[hit.value for hit in page.hits] for page in pages] == [
        ["cel0", "cel1"],
        ["cel2", "a cel0"],
        ["a cel1", "a cel2"],
    ]
    assert [page.more for page in pages] == [True, True, False]


async def test_word_filters(data_dir: Path) -> None:
    """Check that the property, type and entailment filters choose word lanes as they do whole-value ones."""
    settings = make_settings(NAME_PROPERTIES=["rdfs:label", "skos:prefLabel"])
    values = [
        ("label", LABEL, LABEL, '"a cell"@en'),
        ("pref", PREF_LABEL, PREF_LABEL, '"b cell"@en'),
        ("entailed", LABEL, SUB, '"c cell"@en'),
        ("prop", LABEL, LABEL, '"d cell"@en'),
    ]
    kinds = [("label", "Class"), ("pref", "Class"), ("entailed", "Class"), ("prop", "AnnotationProperty")]
    await build(settings, values, kinds)
    assert found(settings, "cell") == ["label", "prop", "entailed", "pref"]
    assert found(settings, "cell", properties=frozenset({f"{SKOS}prefLabel"})) == ["pref"]
    names = frozenset({RDFS_LABEL, f"{SKOS}prefLabel"})
    assert found(settings, "cell", properties=names, entailed=False) == ["label", "prop", "pref"]
    assert found(settings, "cell", types=frozenset({EntityType.ANNOTATION_PROPERTY})) == ["prop"]


async def test_word_rows_one_per_language_rank(data_dir: Path) -> None:
    """Check that a value in two languages that rank alike gets one word row, as it gets one whole-value row."""
    settings = make_settings(LANGUAGE_RANKING=["en"])
    await build(settings, [("x", LABEL, LABEL, '"T-cell"@de'), ("x", LABEL, LABEL, '"T-cell"@fr')])
    with closing(connect(settings, 1, HOOKS_USED)) as connection:
        rows = connection.execute("SELECT word, language FROM search_value ORDER BY word").fetchall()
    assert rows == [(0, "de"), (1, "de")]


async def test_exact_match_first(data_dir: Path) -> None:
    """Check that an exact match ranks first, even under a later name property and a worse language (rule 1)."""
    settings = make_settings(NAME_PROPERTIES=["rdfs:label", "skos:prefLabel"])
    await build(settings, [("prefix", LABEL, LABEL, '"bones"@en'), ("exact", PREF_LABEL, PREF_LABEL, '"Bone"@de')])
    results = find(settings, "bone")
    assert [(hit.iri, hit.exact) for hit in results.hits] == [(f"{EX}exact", True), (f"{EX}prefix", False)]


async def test_property_position(data_dir: Path) -> None:
    """Check that a value of an earlier name property ranks first, even if it's longer (rule 3)."""
    settings = make_settings(NAME_PROPERTIES=["skos:prefLabel", "rdfs:label"])
    await build(settings, [("label", LABEL, LABEL, '"cat"@en'), ("pref", PREF_LABEL, PREF_LABEL, '"catalogue"@en')])
    assert found(settings, "ca") == ["pref", "label"]


async def test_language_position(data_dir: Path) -> None:
    """Check that values rank by language: listed first, then unlisted, then untagged (rule 4, O-5)."""
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
    """Check that a shorter value ranks first (rule 6), by the normalized value's length."""
    settings = make_settings()
    await build(settings, [("longer", LABEL, LABEL, '"abcd"@en'), ("shorter", LABEL, LABEL, '"abc"@en')])
    assert found(settings, "ab") == ["shorter", "longer"]


async def test_alphabetical(data_dir: Path) -> None:
    """Check that values of a length rank by normalized value, then by value (rule 7), across entity types."""
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
    """Check that the rules after exact match apply in order: property, then language, then direct (rules 3-5)."""
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


async def test_long_word_values_bound_the_walk(data_dir: Path) -> None:
    """Check that a long value with word starts makes a query seek at most `KEY_CHARS` lengths a lane, word ones too."""
    settings = make_settings()
    await build(settings, [("long", LABEL, LABEL, f'"b x{"x" * 10_000}"@en'), ("short", LABEL, LABEL, '"ab"@en')])
    assert found(settings, "a") == ["short"]
    assert seeks(settings, "a") <= 2 * KEY_CHARS  # a whole-value lane and a word lane


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
    """Check that a query matches the start of a whole value or of a word, and that leading whitespace is ignored."""
    settings = make_settings()
    await build(settings, [("x", LABEL, LABEL, '"red blood cell"@en'), ("y", LABEL, LABEL, '" \\nHomo sapiens"@en')])
    assert found(settings, "lood") == []
    assert [(hit.value, hit.exact, hit.word_start) for hit in find(settings, "blood").hits] == [
        ("red blood cell", False, 4)
    ]
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
    names = frozenset({RDFS_LABEL, f"{SKOS}prefLabel"})
    assert found(settings, "k", properties=names, entailed=False) == ["class", "property", "pref"]
    assert found(settings, "k", types=frozenset({EntityType.CLASS}), properties=names, entailed=False) == [
        "class",
        "pref",
    ]
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


def test_search_hook_after_values() -> None:
    """Check that the search hook runs after the annotation values hook, whose `annotation_value` it reads."""
    assert HOOKS.index(search_hook) > HOOKS.index(annotation_values) > HOOKS.index(entities)


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


XREF = "<http://www.geneontology.org/formats/oboInOwl#hasDbXref>"
XREF_IRI = XREF.strip("<>")


async def build_common(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Build 20 classes, each labelled, 15 with the cross-reference `GC_ID:1` and one with `PMID:1`.

    With `COMMON_MIN` 5, `GC_ID:1` is common: on more than 10% of the 21 entities (with the ontology), and at least 5.
    """
    monkeypatch.setattr(search_module, "COMMON_MIN", 5)
    values = [(f"c{n}", LABEL, LABEL, f'"class {n}"@en') for n in range(20)]
    values += [(f"c{n}", XREF, XREF, '"GC_ID:1"') for n in range(15)]
    values.append(("c19", XREF, XREF, '"PMID:1"'))
    await build(settings, values)


async def test_common_value_counted(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a common value gets no index rows, only its counts by type; a rarer one is indexed (§2.6)."""
    settings = make_settings()
    await build_common(settings, monkeypatch)
    with closing(connect(settings, 1, HOOKS_USED)) as connection:
        common = connection.execute(
            "SELECT p.iri, value, type, normalized, entities, of_type FROM common_value"
            " JOIN search_property AS p ON p.id = common_value.property"
        ).fetchall()
        rows = connection.execute("SELECT count(*) FROM search_value WHERE value = 'GC_ID:1'").fetchone()[0]
    assert common == [(XREF_IRI, "GC_ID:1", "class", "gc_id:1", 15, 20)]
    assert rows == 0
    assert found(settings, "pmid", properties=frozenset({XREF_IRI})) == ["c19"]


@pytest.mark.parametrize(
    ("text", "filters", "expected"),
    [
        ("gc", {"properties": frozenset({XREF_IRI})}, [("GC_ID:1", False)]),
        ("GC_ID:1", {"properties": frozenset({XREF_IRI})}, [("GC_ID:1", True)]),
        ("id", {"properties": frozenset({XREF_IRI})}, [("GC_ID:1", False)]),  # from a later word
        ("d", {"properties": frozenset({XREF_IRI})}, []),  # not inside a word
        ("gc", {}, []),  # not searched by default
        ("gc", {"properties": frozenset({XREF_IRI}), "types": frozenset({EntityType.OBJECT_PROPERTY})}, []),
        ("gc", {"properties": frozenset({XREF_IRI}), "offset": 10}, []),  # with the first page only
    ],
    ids=["prefix", "exact", "word", "inside", "default", "type", "later page"],
)
async def test_common_value_found(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    filters: dict[str, object],
    expected: list[tuple[str, bool]],
) -> None:
    """Check that a query finds a common value as indexed values match, with its counts, never as hits."""
    settings = make_settings()
    await build_common(settings, monkeypatch)
    results = find(settings, text, **filters)
    assert [(common.value, common.exact) for common in results.common] == expected
    assert all(common.counts == ((EntityType.CLASS, 15, 20),) for common in results.common)
    assert all(hit.value != "GC_ID:1" for hit in results.hits)


async def build_shared(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, classes: int, on: int, common_min: int, languages: int = 1
) -> None:
    """Build labelled classes, `on` of them with the cross-reference `X:1`, in this many languages, and `COMMON_MIN`."""
    monkeypatch.setattr(search_module, "COMMON_MIN", common_min)
    values = [(f"c{n}", LABEL, LABEL, f'"class {n}"@en') for n in range(classes)]
    tags = ["en", "fr", "de"][:languages]
    values += [(f"c{n}", XREF, XREF, f'"X:1"@{tag}') for n in range(on) for tag in tags]
    await build(settings, values)


@pytest.mark.parametrize(
    ("classes", "on", "common_min", "languages", "common"),
    [
        (20, 4, 5, 1, False),  # on fewer than COMMON_MIN
        (20, 5, 5, 1, True),
        (29, 3, 1, 1, False),  # on exactly 10% of the 30 entities, with the ontology
        (29, 4, 1, 1, True),
        (20, 3, 5, 2, False),  # 6 values, but on 3 entities
    ],
    ids=["under the minimum", "at the minimum", "at the share", "over the share", "two languages"],
)
async def test_common_value_threshold(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    classes: int,
    on: int,
    common_min: int,
    languages: int,
    common: bool,
) -> None:
    """Check which values are common: on at least `COMMON_MIN` entities and more than `COMMON_SHARE` of them.

    Entities count, not values: a value in two languages on an entity counts once.
    """
    settings = make_settings()
    await build_shared(settings, monkeypatch, classes, on, common_min, languages)
    results = find(settings, "x:1", properties=frozenset({XREF_IRI}))
    assert [(found.value, found.counts) for found in results.common] == (
        [("X:1", ((EntityType.CLASS, on, classes),))] if common else []
    )
    assert len(results.hits) == (0 if common else on)


async def test_id_in_two_languages_searched_by_default(data_dir: Path) -> None:
    """Check that a property whose values are each on one entity, in two languages, is searched by default.

    Its reuse counts entities, not values (`NAME_REUSE`).
    """
    settings = make_settings()
    ident = f"<{EX}id>"
    await build(settings, [(f"c{n}", ident, ident, f'"ID:{n}"@{tag}') for n in range(3) for tag in ("en", "fr")])
    with closing(connect(settings, 1, HOOKS_USED)) as connection:
        defaults = {p.iri: p.by_default for p in search_properties(connection)}
    assert defaults[f"{EX}id"]
    assert found(settings, "id:1") == ["c1"]


def test_property_labels() -> None:
    """Check a property's label: its display name, else its CURIE by the longest namespace, else its IRI.

    And that a property's count of its sub-properties' values is transitive, as `sub_property` is.
    """
    connection = sqlite3.connect(":memory:")
    connection.executescript(SCHEMA)
    connection.executemany(
        "INSERT INTO prefix VALUES (?, ?)", [("owl", "http://www.w3.org/2002/07/owl#"), ("ex", EX), ("exa", f"{EX}a/")]
    )
    connection.execute(
        "INSERT INTO entity (iri, type, path, display_name) VALUES (?, 'annotation_property', '', ?)",
        (f"{EX}named", "a name"),
    )
    rows = [
        (1, "http://www.w3.org/2002/07/owl#versionInfo", 1),
        (2, f"{EX}named", 0),
        (3, f"{EX}a/sub", 5),
        (4, f"{EX}a/subsub", 7),
        (5, "urn:x", 2),
    ]
    connection.executemany("INSERT INTO search_property VALUES (?, ?, 0, 0, ?)", rows)
    connection.executemany("INSERT INTO sub_property VALUES (?, ?)", [(2, 3), (2, 4), (3, 4)])
    found = {p.iri: (p.label, p.sub_values) for p in search_properties(connection)}
    assert found == {
        "http://www.w3.org/2002/07/owl#versionInfo": ("owl:versionInfo", 0),
        f"{EX}named": ("a name", 12),
        f"{EX}a/sub": ("exa:sub", 7),
        f"{EX}a/subsub": ("exa:subsub", 0),
        "urn:x": ("urn:x", 0),
    }
