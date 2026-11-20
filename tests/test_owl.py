"""Tests for the tree of OWL nodes: their tokens and text, JSON round trips, walks, and the axioms template's HTML."""

import pytest
from pydantic import TypeAdapter

from browl import owl
from browl.links import EntityLink, no_link
from browl.owl import EntityKind, Token, TokenKind
from browl.pages import MAX_IRI_CHARS, MAX_LITERAL_CHARS, templates
from browl.rdf import OWL, RDF, RDFS, XSD, XSD_STRING

NS = "http://example.org/"
A, B, C = (owl.Entity(kind=EntityKind.CLASS, iri=f"{NS}{name}") for name in "ABC")
P, Q = (owl.Entity(kind=EntityKind.OBJECT_PROPERTY, iri=f"{NS}{name}") for name in "pq")
D = owl.Entity(kind=EntityKind.DATA_PROPERTY, iri=f"{NS}d")
IND_I, IND_J = (owl.Entity(kind=EntityKind.NAMED_INDIVIDUAL, iri=f"{NS}{name}") for name in "ij")
X = owl.AnonymousIndividual(id="_:x")
INTEGER = owl.Entity(kind=EntityKind.DATATYPE, iri=f"{XSD}integer")
DT = owl.Entity(kind=EntityKind.DATATYPE, iri=f"{NS}DT")
SOME = owl.ObjectSomeValuesFrom(property=P, filler=A)
NOT_B = owl.ObjectComplementOf(operand=B)
ANY_AXIOM = TypeAdapter(owl.AnyAxiom)


def short(node: owl.Node) -> str:
    """Return a node's text, with `NS` and the XSD namespace left out of its IRIs."""
    return owl.manchester_text(node.manchester_tokens()).replace(NS, "").replace(XSD, "xsd:")


def literal(lexical: str, datatype: str = XSD_STRING, language: str | None = None) -> owl.Literal:
    """Return a literal node."""
    return owl.Literal(lexical=lexical, datatype=datatype, language=language)


@pytest.mark.parametrize(
    ("node", "text"),
    [
        (owl.ObjectIntersectionOf(operands=(A, SOME, NOT_B)), "<A> and (<p> some <A>) and (not (<B>))"),
        (owl.ObjectUnionOf(operands=(A, owl.ObjectIntersectionOf(operands=(B, C)))), "<A> or (<B> and <C>)"),
        (owl.ObjectComplementOf(operand=SOME), "not (<p> some <A>)"),
        (owl.ObjectComplementOf(operand=NOT_B), "not (not (<B>))"),
        (owl.ObjectSomeValuesFrom(property=P, filler=owl.ObjectOneOf(items=(IND_I, X))), "<p> some ({<i>, _:x})"),
        (
            owl.ObjectAllValuesFrom(property=owl.ObjectInverseOf(property=P), filler=SOME),
            "inverse (<p>) only (<p> some <A>)",
        ),
        (owl.ObjectHasValue(property=P, filler=IND_I), "<p> value <i>"),
        (owl.ObjectHasSelf(property=P), "<p> Self"),
        (owl.ObjectMinCardinality(property=P, cardinality=2, filler=NOT_B), "<p> min 2 (not (<B>))"),
        (owl.ObjectMaxCardinality(property=P, cardinality=0, filler=A), "<p> max 0 <A>"),
        (owl.ObjectExactCardinality(property=P, cardinality=1, filler=A), "<p> exactly 1 <A>"),
        (
            owl.DataSomeValuesFrom(property=D, filler=owl.DataComplementOf(operand=INTEGER)),
            "<d> some not <xsd:integer>",
        ),
        (
            owl.DataAllValuesFrom(
                property=D, filler=owl.DataIntersectionOf(operands=(owl.DataUnionOf(operands=(INTEGER, DT)), DT))
            ),
            "<d> only ((<xsd:integer> or <DT>) and <DT>)",
        ),
        (
            owl.DataSomeValuesFrom(
                property=D, filler=owl.DataComplementOf(operand=owl.DataIntersectionOf(operands=(INTEGER, DT)))
            ),
            "<d> some not ((<xsd:integer> and <DT>))",
        ),
        (owl.DataHasValue(property=D, filler=literal("x", language="en")), '<d> value "x"@en'),
        (
            owl.DataMinCardinality(property=D, cardinality=1, filler=owl.DataOneOf(items=(literal("a"), literal("b")))),
            '<d> min 1 {"a", "b"}',
        ),
        (owl.DataMaxCardinality(property=D, cardinality=1, filler=INTEGER), "<d> max 1 <xsd:integer>"),
        (
            owl.DataExactCardinality(
                property=D,
                cardinality=1,
                filler=owl.DatatypeRestriction(
                    datatype=INTEGER,
                    restrictions=(
                        owl.FacetRestriction(facet=f"{XSD}minInclusive", value=literal("0", f"{XSD}integer")),
                        owl.FacetRestriction(facet=f"{XSD}pattern", value=literal("[0-9]+")),
                    ),
                ),
            ),
            '<d> exactly 1 <xsd:integer>[>= 0, pattern "[0-9]+"]',
        ),
    ],
)
def test_expressions(node: owl.Node, text: str) -> None:
    """Check expressions' text: nested class expressions in parentheses, data ranges as the OWLAPI writes them."""
    assert short(node) == text


@pytest.mark.parametrize(
    ("axiom", "text"),
    [
        (owl.SubClassOf(sub=owl.ObjectUnionOf(operands=(A, B)), super=NOT_B), "<A> or <B> SubClassOf not (<B>)"),
        (owl.EquivalentClasses(members=(A, SOME)), "<A> EquivalentTo <p> some <A>"),
        (owl.EquivalentClasses(members=(A, B, SOME)), "EquivalentClasses: <A>, <B>, <p> some <A>"),
        (owl.DisjointClasses(members=(A, B)), "<A> DisjointWith <B>"),
        (owl.DisjointUnion(class_=A, members=(B, C)), "<A> DisjointUnionOf <B>, <C>"),
        (owl.HasKey(class_=SOME, object_properties=(P, Q), data_properties=(D,)), "<p> some <A> HasKey <p>, <q>, <d>"),
        (owl.HasKey(class_=A, object_properties=(), data_properties=(D,)), "<A> HasKey <d>"),
        (owl.SubObjectPropertyOf(sub=owl.ObjectInverseOf(property=P), super=Q), "inverse (<p>) SubPropertyOf: <q>"),
        (owl.SubPropertyChainOf(chain=(P, Q, P), super=Q), "<p> o <q> o <p> SubPropertyOf: <q>"),
        (owl.EquivalentObjectProperties(members=(P, Q)), "<p> EquivalentTo <q>"),
        (owl.EquivalentDataProperties(members=(D, D)), "EquivalentProperties: <d>, <d>"),
        (owl.DisjointObjectProperties(members=(P, Q, P)), "DisjointProperties: <p>, <q>, <p>"),
        (owl.InverseObjectProperties(first=P, second=Q), "<p> InverseOf <q>"),
        (owl.ObjectPropertyDomain(property=P, domain=A), "<p> Domain <A>"),
        (owl.ObjectPropertyRange(property=P, range=A), "<p> Range <A>"),
        (owl.DataPropertyRange(property=D, range=INTEGER), "<d> Range: <xsd:integer>"),
        (owl.TransitiveObjectProperty(property=P), "Transitive: <p>"),
        (owl.FunctionalDataProperty(property=D), "Functional: <d>"),
        (owl.DatatypeDefinition(datatype=DT, range=INTEGER), "<DT> EquivalentTo <xsd:integer>"),
        (owl.ClassAssertion(individual=X, class_=SOME), "_:x Type <p> some <A>"),
        (owl.ObjectPropertyAssertion(subject=IND_I, property=P, object=IND_J), "<i> <p> <j>"),
        (owl.NegativeObjectPropertyAssertion(subject=IND_I, property=P, object=IND_J), "not (<i> <p> <j>)"),
        (owl.NegativeDataPropertyAssertion(subject=IND_I, property=D, object=literal("x")), 'not (<i> <d> "x")'),
        (owl.SameIndividual(members=(IND_I, IND_J)), "<i> SameAs <j>"),
        (owl.DifferentIndividuals(members=(IND_I, IND_J, X)), "DifferentIndividuals: <i>, <j>, _:x"),
        (
            owl.AnnotationPropertyDomain(
                property=owl.Entity(kind=EntityKind.ANNOTATION_PROPERTY, iri=f"{NS}n"),
                domain=owl.IRIReference(iri=f"{NS}A"),
            ),
            "<n> Domain <A>",
        ),
        (owl.OtherAxiom(axiom_type="DLSafeRule", functional="DLSafeRule(Body() Head())"), "DLSafeRule(Body() Head())"),
    ],
)
def test_axioms(axiom: owl.Axiom, text: str) -> None:
    """Check axioms' text: their operands never in parentheses, n-ary ones as pairs or lists."""
    assert short(axiom) == text
    assert axiom.manchester == owl.manchester_text(axiom.manchester_tokens())


def test_tokens() -> None:
    """Check an axiom's tokens: their kinds, IRIs and spacing."""
    axiom = owl.SubClassOf(sub=A, super=owl.ObjectSomeValuesFrom(property=P, filler=owl.ObjectOneOf(items=(IND_I, X))))
    assert axiom.manchester_tokens() == [
        Token(TokenKind.IRI, f"<{NS}A>", f"{NS}A"),
        Token(TokenKind.KEYWORD, "SubClassOf", space=True),
        Token(TokenKind.IRI, f"<{NS}p>", f"{NS}p", space=True),
        Token(TokenKind.KEYWORD, "some", space=True),
        Token(TokenKind.PUNCTUATION, "(", space=True),
        Token(TokenKind.PUNCTUATION, "{"),
        Token(TokenKind.IRI, f"<{NS}i>", f"{NS}i"),
        Token(TokenKind.PUNCTUATION, ","),
        Token(TokenKind.BLANK_NODE, "_:x", space=True),
        Token(TokenKind.PUNCTUATION, "}"),
        Token(TokenKind.PUNCTUATION, ")"),
    ]


@pytest.mark.parametrize(
    ("node", "tokens"),
    [
        (literal("x"), [(TokenKind.LITERAL, '"x"')]),
        (literal("x", f"{RDF}PlainLiteral"), [(TokenKind.LITERAL, '"x"')]),
        (literal("x", f"{RDF}langString", "en-GB"), [(TokenKind.LITERAL, '"x"@en-GB')]),
        (literal('a "b" \\c\n'), [(TokenKind.LITERAL, '"a \\"b\\" \\\\c\n"')]),
        (literal("-5", f"{XSD}integer"), [(TokenKind.LITERAL, "-5")]),
        (literal("1.50", f"{XSD}decimal"), [(TokenKind.LITERAL, "1.50")]),
        (literal("2.5E3", f"{XSD}float"), [(TokenKind.LITERAL, "2.5E3f")]),
        (literal("true", f"{XSD}boolean"), [(TokenKind.LITERAL, "true")]),
        (
            literal("1.5", f"{XSD}double"),
            [(TokenKind.LITERAL, '"1.5"'), (TokenKind.PUNCTUATION, "^^"), (TokenKind.IRI, f"<{XSD}double>")],
        ),
        (
            literal("x", f"{NS}DT"),
            [(TokenKind.LITERAL, '"x"'), (TokenKind.PUNCTUATION, "^^"), (TokenKind.IRI, f"<{NS}DT>")],
        ),
        # Ill-typed, so quoted, where the OWLAPI writes it unquoted.
        (
            literal("5 and <http://example.org/B>", f"{XSD}integer"),
            [
                (TokenKind.LITERAL, '"5 and <http://example.org/B>"'),
                (TokenKind.PUNCTUATION, "^^"),
                (TokenKind.IRI, f"<{XSD}integer>"),
            ],
        ),
        (owl.DataHasValue(property=D, filler=literal("yes", f"{XSD}boolean")), None),
    ],
    ids=[
        "string",
        "plain",
        "language",
        "escapes",
        "integer",
        "decimal",
        "float",
        "boolean",
        "double",
        "custom",
        "ill-typed",
        "ill-typed-boolean",
    ],
)
def test_literals(node: owl.Node, tokens: list[tuple[TokenKind, str]] | None) -> None:
    """Check literals: unquoted when the OWLAPI writes them so and their lexical form is valid, else quoted."""
    if tokens is None:
        assert short(node) == '<d> value "yes"^^<xsd:boolean>'
        return
    assert [(token.kind, token.text) for token in node.manchester_tokens()] == tokens
    assert owl.manchester_text(node.manchester_tokens()) == "".join(text for _, text in tokens)


def test_iris() -> None:
    """Check that `iris` lists the IRIs of entities and literals' datatypes, each once, in the order of the fields."""
    axiom = owl.SubClassOf(
        sub=owl.ObjectIntersectionOf(operands=(B, owl.DataHasValue(property=D, filler=literal("1", f"{NS}DT")))),
        super=owl.ObjectUnionOf(operands=(A, B)),
    )
    assert axiom.iris() == (f"{NS}B", f"{NS}d", f"{NS}DT", f"{NS}A")
    restriction = owl.FacetRestriction(facet=f"{NS}facet", value=literal("1"))
    assert restriction.iris() == (f"{NS}facet", XSD_STRING)
    assert short(restriction) == '<facet> "1"'


AXIOMS = [
    owl.SubClassOf(sub=A, super=owl.ObjectSomeValuesFrom(property=owl.ObjectInverseOf(property=P), filler=NOT_B)),
    owl.HasKey(class_=A, object_properties=(P,), data_properties=(D,)),
    owl.ClassAssertion(individual=X, class_=owl.ObjectOneOf(items=(IND_I, IND_J))),
    owl.DataPropertyAssertion(subject=IND_I, property=D, object=literal("x", language="fr")),
    owl.SubClassOf(
        sub=A,
        super=owl.DataSomeValuesFrom(
            property=D,
            filler=owl.DatatypeRestriction(
                datatype=INTEGER,
                restrictions=(owl.FacetRestriction(facet=f"{XSD}maxExclusive", value=literal("9", f"{XSD}integer")),),
            ),
        ),
    ),
    owl.DisjointUnion(class_=A, members=(B, C)),
    owl.OtherAxiom(axiom_type="DLSafeRule", functional="DLSafeRule(Body() Head())"),
]


@pytest.mark.parametrize("axiom", AXIOMS, ids=lambda axiom: axiom.kind)
def test_json_round_trip(axiom: owl.Axiom) -> None:
    """Check an axiom's JSON round trip, with its Manchester text, and that the discriminator picks its class."""
    dumped = axiom.model_dump(mode="json")
    assert dumped["manchester"] == axiom.manchester
    assert dumped["type"] == type(axiom).__name__
    assert ANY_AXIOM.validate_json(axiom.model_dump_json()) == axiom
    assert type(ANY_AXIOM.validate_python(dumped)) is type(axiom)


def test_cardinality() -> None:
    """Check that a cardinality is a non-negative integer, as OWL 2 says: 0 is valid, -1 isn't."""
    assert short(owl.DataExactCardinality(property=D, cardinality=0, filler=INTEGER)) == "<d> exactly 0 <xsd:integer>"
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        owl.ObjectMinCardinality(property=P, cardinality=-1, filler=A)
    dumped = owl.SubClassOf(sub=A, super=owl.ObjectMaxCardinality(property=P, cardinality=1, filler=A)).model_dump()
    dumped["super"]["cardinality"] = -1
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        ANY_AXIOM.validate_python(dumped)


@pytest.mark.parametrize("iri", ["relative", "http://example.org/a b", "http://example.org/<a>", ""])
def test_invalid_iri(iri: str) -> None:
    """Check that every field holding an IRI refuses one that isn't absolute or has characters IRIs don't allow."""
    with pytest.raises(ValueError, match="not a valid IRI"):
        owl.Entity(kind=EntityKind.CLASS, iri=iri)
    with pytest.raises(ValueError, match="not a valid IRI"):
        owl.IRIReference(iri=iri)
    with pytest.raises(ValueError, match="not a valid IRI"):
        literal("x", iri)
    with pytest.raises(ValueError, match="not a valid IRI"):
        owl.FacetRestriction(facet=iri, value=literal("1"))
    dumped = owl.SubClassOf(sub=A, super=B).model_dump()
    dumped["sub"]["iri"] = iri
    with pytest.raises(ValueError, match="not a valid IRI"):
        ANY_AXIOM.validate_python(dumped)


def test_json_names() -> None:
    """Check that roles named with Python keywords have plain names in JSON, and are read by either name."""
    axiom = owl.ClassAssertion(individual=IND_I, class_=A)
    dumped = axiom.model_dump()
    assert "class" in dumped
    assert "class_" not in dumped
    assert owl.ClassAssertion.model_validate({"individual": IND_I, "class_": A}) == axiom


def test_json_wrong_type() -> None:
    """Check that the discriminator rejects a node of the wrong kind in a field."""
    with pytest.raises(ValueError, match="does not match any of the expected tags"):
        ANY_AXIOM.validate_python({"type": "SubClassOf", "sub": A.model_dump(), "super": {"type": "Literal"}})
    with pytest.raises(ValueError, match="Input tag 'SubClassOf' found using 'type' does not match"):
        TypeAdapter(owl.ClassExpression).validate_python({"type": "SubClassOf"})


def test_node_types() -> None:
    """Check that each concrete node class has its own `NodeType`, named after it, and every one is used."""
    types = [node_type.model_fields["type"].default for node_type in owl.NODE_TYPES]
    assert sorted(types) == sorted(owl.NodeType)
    for node_type, each in zip(owl.NODE_TYPES, types, strict=True):
        assert each == ("IRI" if node_type is owl.IRIReference else node_type.__name__)


@pytest.mark.parametrize(
    "base",
    [
        owl.Node,
        owl._Named,
        owl.Axiom,
        owl._Boolean,
        owl._DataBoolean,
        owl._OneOf,
        owl._Restriction,
        owl._Cardinality,
        owl._Nary,
    ],
    ids=lambda base: base.__name__,
)
def test_bases_abstract(base: type[owl.Node]) -> None:
    """Check that a base class can't be instantiated or validated, even with a type and fields a subclass has."""
    data = {"type": owl.NodeType.OBJECT_INTERSECTION_OF, "operands": (A, B), "members": (A, B)}
    with pytest.raises(TypeError, match=f"^{base.__name__} is a base class"):
        base(**data)
    with pytest.raises(TypeError, match="is a base class"):
        base.model_validate(data)
    assert owl.ObjectIntersectionOf.model_validate(data).operands == (A, B)


def render(tokens: list[Token], links: dict[str, EntityLink]) -> str:
    """Return tokens as the axioms template renders them."""
    source = '{% from "axioms.html" import manchester %}{{ manchester(tokens, links) }}'
    return templates.env.from_string(source).render(tokens=tokens, links=links)


def test_html() -> None:
    """Check that entities with a page become links, keywords are styled, and the rest is escaped text."""
    a = owl.Entity(kind=EntityKind.CLASS, iri=f"{NS}A")
    axiom = owl.SubClassOf(sub=a, super=owl.DataHasValue(property=D, filler=literal(f"<{NS}A> & <i>", language="en")))
    links = {a.iri: EntityLink(a.iri, "<b>A</b> & \"a\" 'b'", "/A"), D.iri: no_link(D.iri)}
    assert render(axiom.manchester_tokens(), links) == (
        '<a href="/A"><bdi>&lt;b&gt;A&lt;/b&gt; &amp; &#34;a&#34; &#39;b&#39;</bdi></a> '
        '<span class="keyword">SubClassOf</span> '
        f'<code dir="ltr">&lt;{NS}d&gt;</code> '
        '<span class="keyword">value</span> '
        f"<bdi>&#34;&lt;{NS}A&gt; &amp; &lt;i&gt;&#34;@en</bdi>"
    )


def test_html_unknown_iri() -> None:
    """Check that an IRI missing from the links is shown as text, as one with no page is."""
    assert render(A.manchester_tokens(), {}) == f'<code dir="ltr">&lt;{NS}A&gt;</code>'


def test_html_punctuation() -> None:
    """Check brackets, commas, a literal's datatype as a link, and other text, as the HTML shows them."""
    axiom = owl.ClassAssertion(
        individual=X,
        class_=owl.DataMinCardinality(property=D, cardinality=2, filler=owl.DataOneOf(items=(literal("1", DT.iri),))),
    )
    html = render(axiom.manchester_tokens(), {DT.iri: EntityLink(DT.iri, "DT", "/DT")})
    assert html == (
        '_:x <span class="keyword">Type</span> <code dir="ltr">&lt;http://example.org/d&gt;</code> '
        '<span class="keyword">min</span> 2 {<bdi>&#34;1&#34;</bdi>^^<a href="/DT"><bdi>DT</bdi></a>}'
    )


def test_html_other_axiom() -> None:
    """Check that an axiom of another kind is escaped text, with no links."""
    axiom = owl.OtherAxiom(axiom_type="DLSafeRule", functional=f'<script>"{NS}A"</script>')
    assert render(axiom.manchester_tokens(), {A.iri: EntityLink(A.iri, "A", "/A")}) == (
        f"&lt;script&gt;&#34;{NS}A&#34;&lt;/script&gt;"
    )


@pytest.mark.parametrize(
    ("iri", "name"),
    [
        (f"{XSD}integer", "xsd:integer"),
        (f"{RDF}PlainLiteral", "rdf:PlainLiteral"),
        (f"{RDFS}Literal", "rdfs:Literal"),
        (f"{OWL}Thing", "owl:Thing"),
        (XSD, None),  # the namespace itself
        (f"{XSD}a/b", None),  # not a local name
        (f"{XSD}integer#x", None),
        (f"{XSD}1st", None),
        ("http://www.w3.org/2001/XMLSchema", None),
        ("http://example.org/integer", None),
    ],
)
def test_prefixed_name(iri: str, name: str | None) -> None:
    """Check that only IRIs in the standard vocabularies, with a plain local name, have a prefixed name."""
    assert owl.prefixed_name(iri) == name
    assert Token(TokenKind.IRI, f"<{iri}>", iri).prefixed_name == name


def test_html_standard_names() -> None:
    """Check that a standard vocabulary's IRI shows its prefixed name, unless it has a page, when the link wins."""
    thing = owl.Entity(kind=EntityKind.CLASS, iri=f"{OWL}Thing")
    axiom = owl.SubClassOf(
        sub=owl.ObjectIntersectionOf(operands=(thing, A)), super=owl.DataSomeValuesFrom(property=D, filler=INTEGER)
    )
    links = {INTEGER.iri: no_link(INTEGER.iri), thing.iri: EntityLink(thing.iri, "Thing", "/Thing")}
    assert render(axiom.manchester_tokens(), links) == (
        '<a href="/Thing"><bdi>Thing</bdi></a> <span class="keyword">and</span> '
        f'<code dir="ltr">&lt;{NS}A&gt;</code> <span class="keyword">SubClassOf</span> '
        f'<code dir="ltr">&lt;{NS}d&gt;</code> <span class="keyword">some</span> <code dir="ltr">xsd:integer</code>'
    )


def test_html_long() -> None:
    """Check that long literals, IRIs shown as text, and other text are cut short; linked IRIs show their names."""
    long_literal = literal("x" * (MAX_LITERAL_CHARS + 50))
    iri = f"{NS}{'i' * MAX_IRI_CHARS}"
    long_entity = owl.Entity(kind=EntityKind.DATA_PROPERTY, iri=iri)
    axiom = owl.DataPropertyAssertion(subject=IND_I, property=long_entity, object=long_literal)
    other = owl.OtherAxiom(axiom_type="DLSafeRule", functional="o" * (MAX_LITERAL_CHARS + 1))
    assert render(axiom.manchester_tokens(), {IND_I.iri: EntityLink(IND_I.iri, "i", "/i")}) == (
        '<a href="/i"><bdi>i</bdi></a> '
        f'<code dir="ltr">&lt;{iri[: MAX_IRI_CHARS - 2]}…</code> '
        f"<bdi>&#34;{'x' * (MAX_LITERAL_CHARS - 2)}…</bdi>"
    )
    assert render(other.manchester_tokens(), {}) == f"{'o' * (MAX_LITERAL_CHARS - 1)}…"
