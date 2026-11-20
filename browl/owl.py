"""OWL 2 axioms and expressions as a typed tree, rendered in Manchester syntax with full IRIs (DESIGN.md §2.8).

Node types follow the OWL 2 structural specification's names (`SubClassOf`, `ObjectSomeValuesFrom`, …), and an
axiom's fields name its roles (`SubClassOf.sub`, `ClassAssertion.individual`). Each node is a frozen Pydantic model with
a `type` field that discriminates the unions of nodes (`ClassExpression`, `AnyAxiom`, …), so nodes round-trip through
JSON (U-42).

A node renders as a stream of typed tokens (`Node.tokens`): keywords, IRIs, literals and punctuation. An axiom's text
(`Axiom.text`) is those tokens joined; templates render the same tokens as HTML, so neither parses text, and links,
literals and keywords come from the tree. A concrete node class declares its fields and its keyword or layout; the
base classes do the rendering, the parentheses and the walks over fields.

The text matches the OWLAPI's Manchester renderer (OWLAPI 4, the one Protégé uses), but for the differences
`tests/test_axioms.py` lists: commas between a `HasKey` axiom's properties and in `{…}` and `[…]` lists, a
`DatatypeDefinition` (which the OWLAPI renders as nothing), single spaces, and literals quoted whenever their lexical
form isn't valid for their datatype. The OWLAPI parenthesizes every class expression nested in another unless it's a
class, and a data range in a complement unless it's a datatype, and so does `Node`.
"""

from __future__ import annotations

import typing
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from browl.names import BUILTIN_PREFIXES
from browl.rdf import RDF, XSD, XSD_LEXICAL_FORMS, XSD_STRING

STANDARD_PREFIXES = {BUILTIN_PREFIXES[prefix]: prefix for prefix in ("xsd", "rdf", "rdfs", "owl")}
"""The standard vocabularies' namespaces, and the prefix each IRI in them is shown with."""

_LOCAL_NAME_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")
_LOCAL_NAME_CHARS = _LOCAL_NAME_START | frozenset("0123456789-")


def prefixed_name(iri: str) -> str | None:
    """Return an IRI's prefixed name if it's in a standard vocabulary (`STANDARD_PREFIXES`), else None."""
    for namespace, prefix in STANDARD_PREFIXES.items():
        local = iri.removeprefix(namespace)
        if local != iri and local[:1] in _LOCAL_NAME_START and _LOCAL_NAME_CHARS.issuperset(local):
            return f"{prefix}:{local}"
    return None


class TokenKind(StrEnum):
    """What a token is."""

    KEYWORD = "keyword"
    IRI = "iri"
    LITERAL = "literal"
    BLANK_NODE = "blank_node"
    PUNCTUATION = "punctuation"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class Token:
    """A piece of Manchester syntax.

    Attributes:
        kind: What it is.
        text: Its text: a keyword with its colon if it has one (`SubPropertyOf:`), an IRI in angle brackets, a literal
            with its quotes and language tag; a number, such as a cardinality, or a facet (`>=`), is `TEXT`.
        iri: An `IRI` token's IRI, else None.
        space: Whether a space comes before it.
    """

    kind: TokenKind
    text: str
    iri: str | None = None
    space: bool = False

    @property
    def prefixed_name(self) -> str | None:
        """Return an IRI token's prefixed name if it's in a standard vocabulary (`xsd:integer`), else None."""
        return None if self.iri is None else prefixed_name(self.iri)


def _keyword(text: str) -> Token:
    return Token(TokenKind.KEYWORD, text)


def _punctuation(text: str) -> Token:
    return Token(TokenKind.PUNCTUATION, text)


_OPEN, _CLOSE, _COMMA = _punctuation("("), _punctuation(")"), _punctuation(",")
_NO_SPACE_AFTER = frozenset({"(", "{", "[", "^^"})
_NO_SPACE_BEFORE = frozenset({")", "}", "]", ",", "[", "^^"})


def _spaced(tokens: Iterable[Token]) -> list[Token]:
    """Return tokens with their spacing: one space between tokens, but none inside brackets or before a comma."""
    result: list[Token] = []
    for token in tokens:
        after = result[-1] if result else None
        space = not (
            after is None
            or (after.kind == TokenKind.PUNCTUATION and after.text in _NO_SPACE_AFTER)
            or (token.kind == TokenKind.PUNCTUATION and token.text in _NO_SPACE_BEFORE)
        )
        result.append(replace(token, space=space) if space != token.space else token)
    return result


def text(tokens: Iterable[Token]) -> str:
    """Return spaced tokens (`Node.tokens`) as text."""
    return "".join(f" {token.text}" if token.space else token.text for token in tokens)


@dataclass(frozen=True, slots=True)
class Slot:
    """A field in a node's layout.

    Attributes:
        name: The field's name.
        separator: The token between the items of a field that holds several.
        nested: Whether an item gets parentheses unless it's an entity, as the OWLAPI gives them.
    """

    name: str
    separator: Token | None = None
    nested: bool = False


type LayoutItem = Token | Slot | Node


class NodeType(StrEnum):
    """A node's type: its class's name, but for `IRIReference`'s; it tells the unions' members apart."""

    ENTITY = "Entity"
    IRI = "IRI"
    ANONYMOUS_INDIVIDUAL = "AnonymousIndividual"
    LITERAL = "Literal"
    OBJECT_INVERSE_OF = "ObjectInverseOf"
    OBJECT_INTERSECTION_OF = "ObjectIntersectionOf"
    OBJECT_UNION_OF = "ObjectUnionOf"
    DATA_INTERSECTION_OF = "DataIntersectionOf"
    DATA_UNION_OF = "DataUnionOf"
    OBJECT_COMPLEMENT_OF = "ObjectComplementOf"
    DATA_COMPLEMENT_OF = "DataComplementOf"
    OBJECT_ONE_OF = "ObjectOneOf"
    DATA_ONE_OF = "DataOneOf"
    OBJECT_SOME_VALUES_FROM = "ObjectSomeValuesFrom"
    OBJECT_ALL_VALUES_FROM = "ObjectAllValuesFrom"
    OBJECT_HAS_VALUE = "ObjectHasValue"
    DATA_SOME_VALUES_FROM = "DataSomeValuesFrom"
    DATA_ALL_VALUES_FROM = "DataAllValuesFrom"
    DATA_HAS_VALUE = "DataHasValue"
    OBJECT_HAS_SELF = "ObjectHasSelf"
    OBJECT_MIN_CARDINALITY = "ObjectMinCardinality"
    OBJECT_MAX_CARDINALITY = "ObjectMaxCardinality"
    OBJECT_EXACT_CARDINALITY = "ObjectExactCardinality"
    DATA_MIN_CARDINALITY = "DataMinCardinality"
    DATA_MAX_CARDINALITY = "DataMaxCardinality"
    DATA_EXACT_CARDINALITY = "DataExactCardinality"
    FACET_RESTRICTION = "FacetRestriction"
    DATATYPE_RESTRICTION = "DatatypeRestriction"
    SUB_CLASS_OF = "SubClassOf"
    EQUIVALENT_CLASSES = "EquivalentClasses"
    DISJOINT_CLASSES = "DisjointClasses"
    DISJOINT_UNION = "DisjointUnion"
    HAS_KEY = "HasKey"
    SUB_OBJECT_PROPERTY_OF = "SubObjectPropertyOf"
    SUB_DATA_PROPERTY_OF = "SubDataPropertyOf"
    SUB_ANNOTATION_PROPERTY_OF = "SubAnnotationPropertyOf"
    SUB_PROPERTY_CHAIN_OF = "SubPropertyChainOf"
    EQUIVALENT_OBJECT_PROPERTIES = "EquivalentObjectProperties"
    EQUIVALENT_DATA_PROPERTIES = "EquivalentDataProperties"
    DISJOINT_OBJECT_PROPERTIES = "DisjointObjectProperties"
    DISJOINT_DATA_PROPERTIES = "DisjointDataProperties"
    INVERSE_OBJECT_PROPERTIES = "InverseObjectProperties"
    OBJECT_PROPERTY_DOMAIN = "ObjectPropertyDomain"
    OBJECT_PROPERTY_RANGE = "ObjectPropertyRange"
    DATA_PROPERTY_DOMAIN = "DataPropertyDomain"
    DATA_PROPERTY_RANGE = "DataPropertyRange"
    ANNOTATION_PROPERTY_DOMAIN = "AnnotationPropertyDomain"
    ANNOTATION_PROPERTY_RANGE = "AnnotationPropertyRange"
    FUNCTIONAL_OBJECT_PROPERTY = "FunctionalObjectProperty"
    INVERSE_FUNCTIONAL_OBJECT_PROPERTY = "InverseFunctionalObjectProperty"
    REFLEXIVE_OBJECT_PROPERTY = "ReflexiveObjectProperty"
    IRREFLEXIVE_OBJECT_PROPERTY = "IrreflexiveObjectProperty"
    SYMMETRIC_OBJECT_PROPERTY = "SymmetricObjectProperty"
    ASYMMETRIC_OBJECT_PROPERTY = "AsymmetricObjectProperty"
    TRANSITIVE_OBJECT_PROPERTY = "TransitiveObjectProperty"
    FUNCTIONAL_DATA_PROPERTY = "FunctionalDataProperty"
    DATATYPE_DEFINITION = "DatatypeDefinition"
    CLASS_ASSERTION = "ClassAssertion"
    OBJECT_PROPERTY_ASSERTION = "ObjectPropertyAssertion"
    NEGATIVE_OBJECT_PROPERTY_ASSERTION = "NegativeObjectPropertyAssertion"
    DATA_PROPERTY_ASSERTION = "DataPropertyAssertion"
    NEGATIVE_DATA_PROPERTY_ASSERTION = "NegativeDataPropertyAssertion"
    SAME_INDIVIDUAL = "SameIndividual"
    DIFFERENT_INDIVIDUALS = "DifferentIndividuals"
    OTHER_AXIOM = "OtherAxiom"


def _is_concrete(node_type: type[Node]) -> bool:
    """Return whether a node class is concrete: it has its own `NodeType`, as its `type` field's default."""
    return isinstance(node_type.model_fields["type"].default, NodeType)


class Node(BaseModel):
    """A node of the tree: an axiom, an expression, an entity or a literal.

    Only a concrete node class, one with its own `NodeType`, can be instantiated or validated; `Node`, `Axiom` and the
    intermediate bases (`_Boolean`, `_Nary`, …) raise `TypeError`, as an abstract class does.

    Attributes:
        type: The node's type.
    """

    model_config = ConfigDict(frozen=True, validate_by_name=True, validate_by_alias=True, serialize_by_alias=True)

    layout: ClassVar[tuple[LayoutItem, ...]] = ()
    """The node's tokens and fields, in order, unless `_layout` says otherwise."""

    type: NodeType

    @model_validator(mode="before")
    @classmethod
    def _concrete_only(cls, data: Any) -> Any:
        """Refuse to make a node of a base class; runs for construction and validation, not `model_construct`."""
        if not _is_concrete(cls):
            raise TypeError(f"{cls.__name__} is a base class; make a node of one of its subclasses")
        return data

    def _layout(self) -> Sequence[LayoutItem]:
        return self.layout

    def _tokens(self) -> Iterator[Token]:
        for item in self._layout():
            if isinstance(item, Token):
                yield item
            elif isinstance(item, Node):
                yield from item._tokens()
            else:
                value = getattr(self, item.name)
                for index, each in enumerate(value if isinstance(value, tuple) else (value,)):
                    if index and item.separator is not None:
                        yield item.separator
                    yield from _child_tokens(each, item.nested)

    def tokens(self) -> list[Token]:
        """Return the node in Manchester syntax, as tokens with their spacing."""
        return _spaced(self._tokens())

    def _iris(self) -> Iterator[str]:
        for name in type(self).model_fields:
            value = getattr(self, name)
            for each in value if isinstance(value, tuple) else (value,):
                if isinstance(each, Node):
                    yield from each._iris()

    def iris(self) -> tuple[str, ...]:
        """Return the IRIs the node mentions, each once, in its fields' order: entities' and literals' datatypes."""
        return tuple(dict.fromkeys(self._iris()))


def _child_tokens(value: Node | int, nested: bool) -> Iterator[Token]:
    if isinstance(value, int):
        yield Token(TokenKind.TEXT, str(value))
        return
    wrap = nested and not isinstance(value, Entity)
    if wrap:
        yield _OPEN
    yield from value._tokens()
    if wrap:
        yield _CLOSE


# Leaves.


class EntityKind(StrEnum):
    """An entity's kind, by its OWL 2 name."""

    CLASS = "Class"
    DATATYPE = "Datatype"
    OBJECT_PROPERTY = "ObjectProperty"
    DATA_PROPERTY = "DataProperty"
    ANNOTATION_PROPERTY = "AnnotationProperty"
    NAMED_INDIVIDUAL = "NamedIndividual"


class Entity(Node):
    """A named entity: a class, datatype, property or named individual.

    Attributes:
        kind: Its kind.
        iri: Its IRI.
    """

    type: typing.Literal[NodeType.ENTITY] = NodeType.ENTITY
    kind: EntityKind
    iri: str

    def _tokens(self) -> Iterator[Token]:
        yield Token(TokenKind.IRI, f"<{self.iri}>", self.iri)

    def _iris(self) -> Iterator[str]:
        yield self.iri


class IRIReference(Node):
    """An IRI that isn't an entity: an annotation property's domain or range.

    Attributes:
        iri: The IRI.
    """

    type: typing.Literal[NodeType.IRI] = NodeType.IRI
    iri: str

    def _tokens(self) -> Iterator[Token]:
        yield Token(TokenKind.IRI, f"<{self.iri}>", self.iri)

    def _iris(self) -> Iterator[str]:
        yield self.iri


class AnonymousIndividual(Node):
    """An anonymous individual (a blank node).

    Attributes:
        id: Its node ID, as the OWLAPI writes it (`_:genid1`).
    """

    type: typing.Literal[NodeType.ANONYMOUS_INDIVIDUAL] = NodeType.ANONYMOUS_INDIVIDUAL
    id: str

    def _tokens(self) -> Iterator[Token]:
        yield Token(TokenKind.BLANK_NODE, self.id)


_PLAIN = frozenset({XSD_STRING, f"{RDF}PlainLiteral"})
"""Datatypes whose literals are written quoted, without a datatype."""
_FLOAT = f"{XSD}float"


class Literal(Node):
    """A literal.

    Attributes:
        lexical: Its lexical form.
        datatype: Its datatype's IRI.
        language: Its language tag, if it has one.
    """

    type: typing.Literal[NodeType.LITERAL] = NodeType.LITERAL
    lexical: str
    datatype: str
    language: str | None = None

    def _tokens(self) -> Iterator[Token]:
        """Yield the literal as the OWLAPI writes it, but quoted whenever its lexical form isn't valid.

        The OWLAPI writes a number or boolean unquoted (`XSD_LEXICAL_FORMS`), else quotes it, with its language tag or
        datatype; it writes an ill-typed number unquoted too, which could pass for IRIs and keywords.
        """
        if self.language:
            yield Token(TokenKind.LITERAL, f"{self.quoted}@{self.language}")
            return
        pattern = XSD_LEXICAL_FORMS.get(self.datatype)
        if pattern is not None and pattern.fullmatch(self.lexical):
            yield Token(TokenKind.LITERAL, f"{self.lexical}f" if self.datatype == _FLOAT else self.lexical)
            return
        yield Token(TokenKind.LITERAL, self.quoted)
        if self.datatype not in _PLAIN:
            yield _punctuation("^^")
            yield Token(TokenKind.IRI, f"<{self.datatype}>", self.datatype)

    @property
    def quoted(self) -> str:
        """Return the lexical form in quotes, with its quotes and backslashes escaped."""
        escaped = self.lexical.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def _iris(self) -> Iterator[str]:
        yield self.datatype


# Property expressions.


class ObjectInverseOf(Node):
    """The inverse of an object property.

    Attributes:
        property: The object property.
    """

    layout = (_keyword("inverse"), _OPEN, Slot("property"), _CLOSE)

    type: typing.Literal[NodeType.OBJECT_INVERSE_OF] = NodeType.OBJECT_INVERSE_OF
    property: Entity


# Class expressions and data ranges.


class _Boolean(Node):
    """An intersection or union: its operands joined by its keyword."""

    keyword: ClassVar[str]

    def _layout(self) -> Sequence[LayoutItem]:
        return (Slot("operands", _keyword(self.keyword), nested=True),)


class _DataBoolean(_Boolean):
    """A data range's intersection or union, which the OWLAPI writes in parentheses wherever it is."""

    def _layout(self) -> Sequence[LayoutItem]:
        return (_OPEN, Slot("operands", _keyword(self.keyword)), _CLOSE)


class ObjectIntersectionOf(_Boolean):
    """An intersection of class expressions (`and`).

    Attributes:
        operands: The class expressions.
    """

    keyword = "and"

    type: typing.Literal[NodeType.OBJECT_INTERSECTION_OF] = NodeType.OBJECT_INTERSECTION_OF
    operands: tuple[ClassExpression, ...]


class ObjectUnionOf(_Boolean):
    """A union of class expressions (`or`).

    Attributes:
        operands: The class expressions.
    """

    keyword = "or"

    type: typing.Literal[NodeType.OBJECT_UNION_OF] = NodeType.OBJECT_UNION_OF
    operands: tuple[ClassExpression, ...]


class DataIntersectionOf(_DataBoolean):
    """An intersection of data ranges (`and`).

    Attributes:
        operands: The data ranges.
    """

    keyword = "and"

    type: typing.Literal[NodeType.DATA_INTERSECTION_OF] = NodeType.DATA_INTERSECTION_OF
    operands: tuple[DataRange, ...]


class DataUnionOf(_DataBoolean):
    """A union of data ranges (`or`).

    Attributes:
        operands: The data ranges.
    """

    keyword = "or"

    type: typing.Literal[NodeType.DATA_UNION_OF] = NodeType.DATA_UNION_OF
    operands: tuple[DataRange, ...]


class ObjectComplementOf(Node):
    """A class expression's complement (`not`), its operand in parentheses, as the OWLAPI writes it.

    Attributes:
        operand: The class expression.
    """

    layout = (_keyword("not"), _OPEN, Slot("operand"), _CLOSE)

    type: typing.Literal[NodeType.OBJECT_COMPLEMENT_OF] = NodeType.OBJECT_COMPLEMENT_OF
    operand: ClassExpression


class DataComplementOf(Node):
    """A data range's complement (`not`).

    Attributes:
        operand: The data range.
    """

    layout = (_keyword("not"), Slot("operand", nested=True))

    type: typing.Literal[NodeType.DATA_COMPLEMENT_OF] = NodeType.DATA_COMPLEMENT_OF
    operand: DataRange


class _OneOf(Node):
    """An enumeration: its items in braces, separated by commas."""

    layout = (_punctuation("{"), Slot("items", _COMMA), _punctuation("}"))


class ObjectOneOf(_OneOf):
    """An enumeration of individuals.

    Attributes:
        items: The individuals.
    """

    type: typing.Literal[NodeType.OBJECT_ONE_OF] = NodeType.OBJECT_ONE_OF
    items: tuple[Individual, ...]


class DataOneOf(_OneOf):
    """An enumeration of literals.

    Attributes:
        items: The literals.
    """

    type: typing.Literal[NodeType.DATA_ONE_OF] = NodeType.DATA_ONE_OF
    items: tuple[Literal, ...]


class _Restriction(Node):
    """A restriction on a property: the property, the keyword, and the filler, in parentheses if nested."""

    keyword: ClassVar[str]
    nested: ClassVar[bool] = True
    """Whether the filler gets parentheses unless it's an entity; the OWLAPI never gives a data range, an individual
    or a literal any here."""

    def _layout(self) -> Sequence[LayoutItem]:
        return (Slot("property"), _keyword(self.keyword), Slot("filler", nested=self.nested))


class ObjectSomeValuesFrom(_Restriction):
    """An existential restriction on an object property (`some`).

    Attributes:
        property: The object property expression.
        filler: The class expression.
    """

    keyword = "some"

    type: typing.Literal[NodeType.OBJECT_SOME_VALUES_FROM] = NodeType.OBJECT_SOME_VALUES_FROM
    property: ObjectPropertyExpression
    filler: ClassExpression


class ObjectAllValuesFrom(_Restriction):
    """A universal restriction on an object property (`only`).

    Attributes:
        property: The object property expression.
        filler: The class expression.
    """

    keyword = "only"

    type: typing.Literal[NodeType.OBJECT_ALL_VALUES_FROM] = NodeType.OBJECT_ALL_VALUES_FROM
    property: ObjectPropertyExpression
    filler: ClassExpression


class ObjectHasValue(_Restriction):
    """A value restriction on an object property (`value`).

    Attributes:
        property: The object property expression.
        filler: The individual.
    """

    keyword = "value"
    nested = False

    type: typing.Literal[NodeType.OBJECT_HAS_VALUE] = NodeType.OBJECT_HAS_VALUE
    property: ObjectPropertyExpression
    filler: Individual


class DataSomeValuesFrom(_Restriction):
    """An existential restriction on a data property (`some`).

    Attributes:
        property: The data property.
        filler: The data range.
    """

    keyword = "some"
    nested = False

    type: typing.Literal[NodeType.DATA_SOME_VALUES_FROM] = NodeType.DATA_SOME_VALUES_FROM
    property: Entity
    filler: DataRange


class DataAllValuesFrom(_Restriction):
    """A universal restriction on a data property (`only`).

    Attributes:
        property: The data property.
        filler: The data range.
    """

    keyword = "only"
    nested = False

    type: typing.Literal[NodeType.DATA_ALL_VALUES_FROM] = NodeType.DATA_ALL_VALUES_FROM
    property: Entity
    filler: DataRange


class DataHasValue(_Restriction):
    """A value restriction on a data property (`value`).

    Attributes:
        property: The data property.
        filler: The literal.
    """

    keyword = "value"
    nested = False

    type: typing.Literal[NodeType.DATA_HAS_VALUE] = NodeType.DATA_HAS_VALUE
    property: Entity
    filler: Literal


class ObjectHasSelf(Node):
    """A self-restriction on an object property (`Self`).

    Attributes:
        property: The object property expression.
    """

    layout = (Slot("property"), _keyword("Self"))

    type: typing.Literal[NodeType.OBJECT_HAS_SELF] = NodeType.OBJECT_HAS_SELF
    property: ObjectPropertyExpression


class _Cardinality(_Restriction):
    """A cardinality restriction: the property, the keyword, the number, and the filler, in parentheses if nested.

    The OWLAPI gives a restriction without a filler `owl:Thing`'s, or `rdfs:Literal`'s, so every one has a filler.
    """

    def _layout(self) -> Sequence[LayoutItem]:
        return (Slot("property"), _keyword(self.keyword), Slot("cardinality"), Slot("filler", nested=self.nested))


class ObjectMinCardinality(_Cardinality):
    """A minimum cardinality restriction on an object property (`min`).

    Attributes:
        property: The object property expression.
        cardinality: The number.
        filler: The class expression.
    """

    keyword = "min"

    type: typing.Literal[NodeType.OBJECT_MIN_CARDINALITY] = NodeType.OBJECT_MIN_CARDINALITY
    property: ObjectPropertyExpression
    cardinality: int
    filler: ClassExpression


class ObjectMaxCardinality(_Cardinality):
    """A maximum cardinality restriction on an object property (`max`).

    Attributes:
        property: The object property expression.
        cardinality: The number.
        filler: The class expression.
    """

    keyword = "max"

    type: typing.Literal[NodeType.OBJECT_MAX_CARDINALITY] = NodeType.OBJECT_MAX_CARDINALITY
    property: ObjectPropertyExpression
    cardinality: int
    filler: ClassExpression


class ObjectExactCardinality(_Cardinality):
    """An exact cardinality restriction on an object property (`exactly`).

    Attributes:
        property: The object property expression.
        cardinality: The number.
        filler: The class expression.
    """

    keyword = "exactly"

    type: typing.Literal[NodeType.OBJECT_EXACT_CARDINALITY] = NodeType.OBJECT_EXACT_CARDINALITY
    property: ObjectPropertyExpression
    cardinality: int
    filler: ClassExpression


class DataMinCardinality(_Cardinality):
    """A minimum cardinality restriction on a data property (`min`).

    Attributes:
        property: The data property.
        cardinality: The number.
        filler: The data range.
    """

    keyword = "min"
    nested = False

    type: typing.Literal[NodeType.DATA_MIN_CARDINALITY] = NodeType.DATA_MIN_CARDINALITY
    property: Entity
    cardinality: int
    filler: DataRange


class DataMaxCardinality(_Cardinality):
    """A maximum cardinality restriction on a data property (`max`).

    Attributes:
        property: The data property.
        cardinality: The number.
        filler: The data range.
    """

    keyword = "max"
    nested = False

    type: typing.Literal[NodeType.DATA_MAX_CARDINALITY] = NodeType.DATA_MAX_CARDINALITY
    property: Entity
    cardinality: int
    filler: DataRange


class DataExactCardinality(_Cardinality):
    """An exact cardinality restriction on a data property (`exactly`).

    Attributes:
        property: The data property.
        cardinality: The number.
        filler: The data range.
    """

    keyword = "exactly"
    nested = False

    type: typing.Literal[NodeType.DATA_EXACT_CARDINALITY] = NodeType.DATA_EXACT_CARDINALITY
    property: Entity
    cardinality: int
    filler: DataRange


FACETS = {
    f"{XSD}length": "length",
    f"{XSD}minLength": "minLength",
    f"{XSD}maxLength": "maxLength",
    f"{XSD}pattern": "pattern",
    f"{XSD}minInclusive": ">=",
    f"{XSD}minExclusive": ">",
    f"{XSD}maxInclusive": "<=",
    f"{XSD}maxExclusive": "<",
    f"{XSD}totalDigits": "totalDigits",
    f"{XSD}fractionDigits": "fractionDigits",
    f"{RDF}langRange": "langRange",
}
"""Each facet's IRI, and its symbol in Manchester syntax; a test checks them against the OWLAPI's (`OWLFacet`)."""


class FacetRestriction(Node):
    """A constraining facet and its value, in a datatype restriction.

    Attributes:
        facet: The facet's IRI (`FACETS`).
        value: The value.
    """

    type: typing.Literal[NodeType.FACET_RESTRICTION] = NodeType.FACET_RESTRICTION
    facet: str
    value: Literal

    def _tokens(self) -> Iterator[Token]:
        symbol = FACETS.get(self.facet)
        yield Token(TokenKind.TEXT, symbol) if symbol else Token(TokenKind.IRI, f"<{self.facet}>", self.facet)
        yield from self.value._tokens()

    def _iris(self) -> Iterator[str]:
        if self.facet not in FACETS:
            yield self.facet
        yield from self.value._iris()


class DatatypeRestriction(Node):
    """A datatype restricted by facets (`xsd:integer[>= 0]`).

    Attributes:
        datatype: The datatype.
        restrictions: The facets and their values.
    """

    layout = (Slot("datatype"), _punctuation("["), Slot("restrictions", _COMMA), _punctuation("]"))

    type: typing.Literal[NodeType.DATATYPE_RESTRICTION] = NodeType.DATATYPE_RESTRICTION
    datatype: Entity
    restrictions: tuple[FacetRestriction, ...]


# Axioms.


class Axiom(Node):
    """An axiom, without its annotations: what a page shows of it (DESIGN.md §2.8, Panel data apart from its HTML)."""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def text(self) -> str:
        """Return the axiom in Manchester syntax, on one line, every entity in it written as its full IRI, `<…>`."""
        return text(self.tokens())

    @property
    def kind(self) -> str:
        """Return the axiom's kind, by its OWL 2 functional-syntax name, as `type` does; but see `OtherAxiom`."""
        return self.type


class SubClassOf(Axiom):
    """A subclass axiom.

    Attributes:
        sub: The subclass.
        super: The superclass.
    """

    layout = (Slot("sub"), _keyword("SubClassOf"), Slot("super"))

    type: typing.Literal[NodeType.SUB_CLASS_OF] = NodeType.SUB_CLASS_OF
    sub: ClassExpression
    super: ClassExpression


class _Nary(Axiom):
    """An n-ary axiom: with two members, `a keyword b`; else a keyword and the members, separated by commas.

    The OWLAPI writes the members sorted, and so does `browl.axioms`.
    """

    members: tuple[Node, ...]

    pair: ClassVar[str | None]
    """The keyword between two members; None to write two members as a list too."""
    listed: ClassVar[str]
    """The keyword before a list of members."""

    def _layout(self) -> Sequence[LayoutItem]:
        members: tuple[Node, ...] = self.members
        if len(members) == 2 and self.pair is not None:
            return (members[0], _keyword(self.pair), members[1])
        return (_keyword(self.listed), Slot("members", _COMMA))


class EquivalentClasses(_Nary):
    """An equivalent-classes axiom.

    Attributes:
        members: The class expressions.
    """

    pair, listed = "EquivalentTo", "EquivalentClasses:"

    type: typing.Literal[NodeType.EQUIVALENT_CLASSES] = NodeType.EQUIVALENT_CLASSES
    members: tuple[ClassExpression, ...]


class DisjointClasses(_Nary):
    """A disjoint-classes axiom.

    Attributes:
        members: The class expressions.
    """

    pair, listed = "DisjointWith", "DisjointClasses:"

    type: typing.Literal[NodeType.DISJOINT_CLASSES] = NodeType.DISJOINT_CLASSES
    members: tuple[ClassExpression, ...]


class DisjointUnion(Axiom):
    """A disjoint-union axiom: a class is the disjoint union of class expressions.

    Attributes:
        class_: The class.
        members: The class expressions.
    """

    layout = (Slot("class_"), _keyword("DisjointUnionOf"), Slot("members", _COMMA))

    type: typing.Literal[NodeType.DISJOINT_UNION] = NodeType.DISJOINT_UNION
    class_: Entity = Field(alias="class")
    members: tuple[ClassExpression, ...]


class HasKey(Axiom):
    """A key axiom: individuals of a class are identified by the values of properties.

    Its properties are separated by commas, where the OWLAPI writes nothing between them.

    Attributes:
        class_: The class expression.
        object_properties: The object property expressions.
        data_properties: The data properties.
    """

    type: typing.Literal[NodeType.HAS_KEY] = NodeType.HAS_KEY
    class_: ClassExpression = Field(alias="class")
    object_properties: tuple[ObjectPropertyExpression, ...]
    data_properties: tuple[Entity, ...]

    def _layout(self) -> Sequence[LayoutItem]:
        items: list[LayoutItem] = [Slot("class_"), _keyword("HasKey")]
        for index, each in enumerate((*self.object_properties, *self.data_properties)):
            items += (_COMMA, each) if index else (each,)
        return items


class _SubPropertyOf(Axiom):
    """A subproperty axiom."""

    layout = (Slot("sub"), _keyword("SubPropertyOf:"), Slot("super"))


class SubObjectPropertyOf(_SubPropertyOf):
    """A subproperty axiom between object property expressions.

    Attributes:
        sub: The subproperty.
        super: The superproperty.
    """

    type: typing.Literal[NodeType.SUB_OBJECT_PROPERTY_OF] = NodeType.SUB_OBJECT_PROPERTY_OF
    sub: ObjectPropertyExpression
    super: ObjectPropertyExpression


class SubDataPropertyOf(_SubPropertyOf):
    """A subproperty axiom between data properties.

    Attributes:
        sub: The subproperty.
        super: The superproperty.
    """

    type: typing.Literal[NodeType.SUB_DATA_PROPERTY_OF] = NodeType.SUB_DATA_PROPERTY_OF
    sub: Entity
    super: Entity


class SubAnnotationPropertyOf(_SubPropertyOf):
    """A subproperty axiom between annotation properties.

    Attributes:
        sub: The subproperty.
        super: The superproperty.
    """

    type: typing.Literal[NodeType.SUB_ANNOTATION_PROPERTY_OF] = NodeType.SUB_ANNOTATION_PROPERTY_OF
    sub: Entity
    super: Entity


class SubPropertyChainOf(Axiom):
    """A property chain that implies an object property.

    OWL 2 writes it `SubObjectPropertyOf(ObjectPropertyChain(…) …)`; the OWLAPI as its own kind of axiom, which P-16
    shows apart.

    Attributes:
        chain: The object property expressions, in order.
        super: The object property expression.
    """

    layout = (Slot("chain", _keyword("o")), _keyword("SubPropertyOf:"), Slot("super"))

    type: typing.Literal[NodeType.SUB_PROPERTY_CHAIN_OF] = NodeType.SUB_PROPERTY_CHAIN_OF
    chain: tuple[ObjectPropertyExpression, ...]
    super: ObjectPropertyExpression


class EquivalentObjectProperties(_Nary):
    """An equivalent-properties axiom between object property expressions.

    Attributes:
        members: The object property expressions.
    """

    pair, listed = "EquivalentTo", "EquivalentProperties:"

    type: typing.Literal[NodeType.EQUIVALENT_OBJECT_PROPERTIES] = NodeType.EQUIVALENT_OBJECT_PROPERTIES
    members: tuple[ObjectPropertyExpression, ...]


class EquivalentDataProperties(_Nary):
    """An equivalent-properties axiom between data properties, which the OWLAPI always writes as a list.

    Attributes:
        members: The data properties.
    """

    pair, listed = None, "EquivalentProperties:"

    type: typing.Literal[NodeType.EQUIVALENT_DATA_PROPERTIES] = NodeType.EQUIVALENT_DATA_PROPERTIES
    members: tuple[Entity, ...]


class DisjointObjectProperties(_Nary):
    """A disjoint-properties axiom between object property expressions.

    Attributes:
        members: The object property expressions.
    """

    pair, listed = "DisjointWith", "DisjointProperties:"

    type: typing.Literal[NodeType.DISJOINT_OBJECT_PROPERTIES] = NodeType.DISJOINT_OBJECT_PROPERTIES
    members: tuple[ObjectPropertyExpression, ...]


class DisjointDataProperties(_Nary):
    """A disjoint-properties axiom between data properties.

    Attributes:
        members: The data properties.
    """

    pair, listed = "DisjointWith", "DisjointProperties:"

    type: typing.Literal[NodeType.DISJOINT_DATA_PROPERTIES] = NodeType.DISJOINT_DATA_PROPERTIES
    members: tuple[Entity, ...]


class InverseObjectProperties(Axiom):
    """An inverse-properties axiom.

    Attributes:
        first: One object property expression.
        second: Its inverse.
    """

    layout = (Slot("first"), _keyword("InverseOf"), Slot("second"))

    type: typing.Literal[NodeType.INVERSE_OBJECT_PROPERTIES] = NodeType.INVERSE_OBJECT_PROPERTIES
    first: ObjectPropertyExpression
    second: ObjectPropertyExpression


class _Domain(Axiom):
    """A property's domain."""

    layout = (Slot("property"), _keyword("Domain"), Slot("domain"))


class _Range(Axiom):
    """A property's range."""

    keyword: ClassVar[str] = "Range"

    def _layout(self) -> Sequence[LayoutItem]:
        return (Slot("property"), _keyword(self.keyword), Slot("range"))


class ObjectPropertyDomain(_Domain):
    """An object property's domain.

    Attributes:
        property: The object property expression.
        domain: The class expression.
    """

    type: typing.Literal[NodeType.OBJECT_PROPERTY_DOMAIN] = NodeType.OBJECT_PROPERTY_DOMAIN
    property: ObjectPropertyExpression
    domain: ClassExpression


class ObjectPropertyRange(_Range):
    """An object property's range.

    Attributes:
        property: The object property expression.
        range: The class expression.
    """

    type: typing.Literal[NodeType.OBJECT_PROPERTY_RANGE] = NodeType.OBJECT_PROPERTY_RANGE
    property: ObjectPropertyExpression
    range: ClassExpression


class DataPropertyDomain(_Domain):
    """A data property's domain.

    Attributes:
        property: The data property.
        domain: The class expression.
    """

    type: typing.Literal[NodeType.DATA_PROPERTY_DOMAIN] = NodeType.DATA_PROPERTY_DOMAIN
    property: Entity
    domain: ClassExpression


class DataPropertyRange(_Range):
    """A data property's range, which the OWLAPI writes `Range:`, with a colon.

    Attributes:
        property: The data property.
        range: The data range.
    """

    keyword = "Range:"

    type: typing.Literal[NodeType.DATA_PROPERTY_RANGE] = NodeType.DATA_PROPERTY_RANGE
    property: Entity
    range: DataRange


class AnnotationPropertyDomain(_Domain):
    """An annotation property's domain.

    Attributes:
        property: The annotation property.
        domain: The IRI.
    """

    type: typing.Literal[NodeType.ANNOTATION_PROPERTY_DOMAIN] = NodeType.ANNOTATION_PROPERTY_DOMAIN
    property: Entity
    domain: IRIReference


class AnnotationPropertyRange(_Range):
    """An annotation property's range.

    Attributes:
        property: The annotation property.
        range: The IRI.
    """

    type: typing.Literal[NodeType.ANNOTATION_PROPERTY_RANGE] = NodeType.ANNOTATION_PROPERTY_RANGE
    property: Entity
    range: IRIReference


class _Characteristic(Axiom):
    """A property characteristic: its keyword, then the property."""

    keyword: ClassVar[str]

    def _layout(self) -> Sequence[LayoutItem]:
        return (_keyword(self.keyword), Slot("property"))


class FunctionalObjectProperty(_Characteristic):
    """An object property is functional.

    Attributes:
        property: The object property expression.
    """

    keyword = "Functional:"

    type: typing.Literal[NodeType.FUNCTIONAL_OBJECT_PROPERTY] = NodeType.FUNCTIONAL_OBJECT_PROPERTY
    property: ObjectPropertyExpression


class InverseFunctionalObjectProperty(_Characteristic):
    """An object property is inverse-functional.

    Attributes:
        property: The object property expression.
    """

    keyword = "InverseFunctional:"

    type: typing.Literal[NodeType.INVERSE_FUNCTIONAL_OBJECT_PROPERTY] = NodeType.INVERSE_FUNCTIONAL_OBJECT_PROPERTY
    property: ObjectPropertyExpression


class ReflexiveObjectProperty(_Characteristic):
    """An object property is reflexive.

    Attributes:
        property: The object property expression.
    """

    keyword = "Reflexive:"

    type: typing.Literal[NodeType.REFLEXIVE_OBJECT_PROPERTY] = NodeType.REFLEXIVE_OBJECT_PROPERTY
    property: ObjectPropertyExpression


class IrreflexiveObjectProperty(_Characteristic):
    """An object property is irreflexive.

    Attributes:
        property: The object property expression.
    """

    keyword = "Irreflexive:"

    type: typing.Literal[NodeType.IRREFLEXIVE_OBJECT_PROPERTY] = NodeType.IRREFLEXIVE_OBJECT_PROPERTY
    property: ObjectPropertyExpression


class SymmetricObjectProperty(_Characteristic):
    """An object property is symmetric.

    Attributes:
        property: The object property expression.
    """

    keyword = "Symmetric:"

    type: typing.Literal[NodeType.SYMMETRIC_OBJECT_PROPERTY] = NodeType.SYMMETRIC_OBJECT_PROPERTY
    property: ObjectPropertyExpression


class AsymmetricObjectProperty(_Characteristic):
    """An object property is asymmetric.

    Attributes:
        property: The object property expression.
    """

    keyword = "Asymmetric:"

    type: typing.Literal[NodeType.ASYMMETRIC_OBJECT_PROPERTY] = NodeType.ASYMMETRIC_OBJECT_PROPERTY
    property: ObjectPropertyExpression


class TransitiveObjectProperty(_Characteristic):
    """An object property is transitive.

    Attributes:
        property: The object property expression.
    """

    keyword = "Transitive:"

    type: typing.Literal[NodeType.TRANSITIVE_OBJECT_PROPERTY] = NodeType.TRANSITIVE_OBJECT_PROPERTY
    property: ObjectPropertyExpression


class FunctionalDataProperty(_Characteristic):
    """A data property is functional.

    Attributes:
        property: The data property.
    """

    keyword = "Functional:"

    type: typing.Literal[NodeType.FUNCTIONAL_DATA_PROPERTY] = NodeType.FUNCTIONAL_DATA_PROPERTY
    property: Entity


class DatatypeDefinition(Axiom):
    """A datatype defined as a data range.

    Written with `EquivalentTo`, as in a Manchester syntax frame; the OWLAPI renders it as nothing.

    Attributes:
        datatype: The datatype.
        range: The data range.
    """

    layout = (Slot("datatype"), _keyword("EquivalentTo"), Slot("range"))

    type: typing.Literal[NodeType.DATATYPE_DEFINITION] = NodeType.DATATYPE_DEFINITION
    datatype: Entity
    range: DataRange


class ClassAssertion(Axiom):
    """An individual is an instance of a class expression.

    Attributes:
        individual: The individual.
        class_: The class expression.
    """

    layout = (Slot("individual"), _keyword("Type"), Slot("class_"))

    type: typing.Literal[NodeType.CLASS_ASSERTION] = NodeType.CLASS_ASSERTION
    individual: Individual
    class_: ClassExpression = Field(alias="class")


class _PropertyAssertion(Axiom):
    """A property assertion: subject, property, object; a negative one in parentheses after `not`."""

    negative: ClassVar[bool] = False

    def _layout(self) -> Sequence[LayoutItem]:
        triple = (Slot("subject"), Slot("property"), Slot("object"))
        return (_keyword("not"), _OPEN, *triple, _CLOSE) if self.negative else triple


class ObjectPropertyAssertion(_PropertyAssertion):
    """An object property assertion.

    Attributes:
        subject: The subject.
        property: The object property expression.
        object: The object.
    """

    type: typing.Literal[NodeType.OBJECT_PROPERTY_ASSERTION] = NodeType.OBJECT_PROPERTY_ASSERTION
    subject: Individual
    property: ObjectPropertyExpression
    object: Individual


class NegativeObjectPropertyAssertion(_PropertyAssertion):
    """A negative object property assertion.

    Attributes:
        subject: The subject.
        property: The object property expression.
        object: The object.
    """

    negative = True

    type: typing.Literal[NodeType.NEGATIVE_OBJECT_PROPERTY_ASSERTION] = NodeType.NEGATIVE_OBJECT_PROPERTY_ASSERTION
    subject: Individual
    property: ObjectPropertyExpression
    object: Individual


class DataPropertyAssertion(_PropertyAssertion):
    """A data property assertion.

    Attributes:
        subject: The subject.
        property: The data property.
        object: The literal.
    """

    type: typing.Literal[NodeType.DATA_PROPERTY_ASSERTION] = NodeType.DATA_PROPERTY_ASSERTION
    subject: Individual
    property: Entity
    object: Literal


class NegativeDataPropertyAssertion(_PropertyAssertion):
    """A negative data property assertion.

    Attributes:
        subject: The subject.
        property: The data property.
        object: The literal.
    """

    negative = True

    type: typing.Literal[NodeType.NEGATIVE_DATA_PROPERTY_ASSERTION] = NodeType.NEGATIVE_DATA_PROPERTY_ASSERTION
    subject: Individual
    property: Entity
    object: Literal


class SameIndividual(_Nary):
    """Individuals are the same.

    Attributes:
        members: The individuals.
    """

    pair, listed = "SameAs", "SameIndividual:"

    type: typing.Literal[NodeType.SAME_INDIVIDUAL] = NodeType.SAME_INDIVIDUAL
    members: tuple[Individual, ...]


class DifferentIndividuals(_Nary):
    """Individuals are different.

    Attributes:
        members: The individuals.
    """

    pair, listed = "DifferentFrom", "DifferentIndividuals:"

    type: typing.Literal[NodeType.DIFFERENT_INDIVIDUALS] = NodeType.DIFFERENT_INDIVIDUALS
    members: tuple[Individual, ...]


class OtherAxiom(Axiom):
    """An axiom of a kind no other node type covers, such as a SWRL rule, as plain text in functional syntax.

    Attributes:
        axiom_type: Its kind, as the OWLAPI names it (`AxiomType`).
        functional: The axiom in functional syntax, as the OWLAPI writes it.
    """

    type: typing.Literal[NodeType.OTHER_AXIOM] = NodeType.OTHER_AXIOM
    axiom_type: str
    functional: str

    def _tokens(self) -> Iterator[Token]:
        yield Token(TokenKind.TEXT, self.functional)

    @property
    def kind(self) -> str:
        """Return the axiom's kind, as the OWLAPI names it."""
        return self.axiom_type


Individual = Annotated[Entity | AnonymousIndividual, Field(discriminator="type")]
ObjectPropertyExpression = Annotated[Entity | ObjectInverseOf, Field(discriminator="type")]
ClassExpression = Annotated[
    Entity
    | ObjectIntersectionOf
    | ObjectUnionOf
    | ObjectComplementOf
    | ObjectOneOf
    | ObjectSomeValuesFrom
    | ObjectAllValuesFrom
    | ObjectHasValue
    | ObjectHasSelf
    | ObjectMinCardinality
    | ObjectMaxCardinality
    | ObjectExactCardinality
    | DataSomeValuesFrom
    | DataAllValuesFrom
    | DataHasValue
    | DataMinCardinality
    | DataMaxCardinality
    | DataExactCardinality,
    Field(discriminator="type"),
]
DataRange = Annotated[
    Entity | DataIntersectionOf | DataUnionOf | DataComplementOf | DataOneOf | DatatypeRestriction,
    Field(discriminator="type"),
]
AnyAxiom = Annotated[
    SubClassOf
    | EquivalentClasses
    | DisjointClasses
    | DisjointUnion
    | HasKey
    | SubObjectPropertyOf
    | SubPropertyChainOf
    | EquivalentObjectProperties
    | DisjointObjectProperties
    | InverseObjectProperties
    | ObjectPropertyDomain
    | ObjectPropertyRange
    | FunctionalObjectProperty
    | InverseFunctionalObjectProperty
    | ReflexiveObjectProperty
    | IrreflexiveObjectProperty
    | SymmetricObjectProperty
    | AsymmetricObjectProperty
    | TransitiveObjectProperty
    | SubDataPropertyOf
    | EquivalentDataProperties
    | DisjointDataProperties
    | DataPropertyDomain
    | DataPropertyRange
    | FunctionalDataProperty
    | DatatypeDefinition
    | ClassAssertion
    | ObjectPropertyAssertion
    | NegativeObjectPropertyAssertion
    | DataPropertyAssertion
    | NegativeDataPropertyAssertion
    | SameIndividual
    | DifferentIndividuals
    | SubAnnotationPropertyOf
    | AnnotationPropertyDomain
    | AnnotationPropertyRange
    | OtherAxiom,
    Field(discriminator="type"),
]
"""Any axiom: what a JSON axiom is read as (`pydantic.TypeAdapter(AnyAxiom)`)."""


def _concrete(base: type[Node]) -> Iterator[type[Node]]:
    for each in base.__subclasses__():
        if _is_concrete(each):
            yield each
        yield from _concrete(each)


NODE_TYPES = tuple(_concrete(Node))
"""Every concrete node class."""

for _node_type in NODE_TYPES:
    _node_type.model_rebuild()
