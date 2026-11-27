"""An entity's logical axioms, grouped into a frame's sections, as typed data the axioms panel renders (DESIGN.md §2.8).

A frame shows each axiom by the operands that aren't the entity, under a section named for its kind: on Heart's page,
`Heart SubClassOf Organ` is `Organ` under SubClass Of. An axiom whose entity isn't where its section expects it, such
as a general class axiom, is shown whole. An individual's property assertions are under their property's name within
their section, as annotations are under theirs (P-47).
"""

import functools
from collections.abc import Callable, Iterable, Iterator, Sequence
from enum import StrEnum
from typing import Annotated, TypeGuard

from pydantic import BaseModel, ConfigDict, Field

from browl import owl
from browl.owl import AnyAxiom, Entity, EntityKind, Token, TokenKind
from browl.rdf import IRI


class Section(StrEnum):
    """A section of the frame (DESIGN.md §2.8): a class's, a property's, an individual's, or one for any other axiom."""

    SUB_CLASS_OF = "sub_class_of"
    EQUIVALENT_TO = "equivalent_to"
    DISJOINT_WITH = "disjoint_with"
    DISJOINT_UNION_OF = "disjoint_union_of"
    KEYS = "keys"
    GENERAL = "general"
    DOMAINS = "domains"
    RANGES = "ranges"
    CHARACTERISTICS = "characteristics"
    SUB_PROPERTY_OF = "sub_property_of"
    INVERSE_OF = "inverse_of"
    CHAINS = "chains"
    TYPES = "types"
    SAME_AS = "same_as"
    DIFFERENT_FROM = "different_from"
    OBJECT_ASSERTIONS = "object_assertions"
    DATA_ASSERTIONS = "data_assertions"
    NEGATIVE_ASSERTIONS = "negative_assertions"
    OTHER = "other"


SECTION_LABELS = {
    Section.SUB_CLASS_OF: "SubClass Of",
    Section.EQUIVALENT_TO: "Equivalent To",
    Section.DISJOINT_WITH: "Disjoint With",
    Section.DISJOINT_UNION_OF: "Disjoint Union Of",
    Section.KEYS: "Keys",
    Section.GENERAL: "General class axioms",
    Section.DOMAINS: "Domains",
    Section.RANGES: "Ranges",
    Section.CHARACTERISTICS: "Characteristics",
    Section.SUB_PROPERTY_OF: "SubProperty Of",
    Section.INVERSE_OF: "Inverse Of",
    Section.CHAINS: "SuperProperty Of (Chain)",
    Section.TYPES: "Types",
    Section.SAME_AS: "Same As",
    Section.DIFFERENT_FROM: "Different From",
    Section.OBJECT_ASSERTIONS: "Object property assertions",
    Section.DATA_ASSERTIONS: "Data property assertions",
    Section.NEGATIVE_ASSERTIONS: "Negative property assertions",
    Section.OTHER: "Other axioms",
}
"""Each section's label, as the page shows it."""

KIND_LABELS = {
    EntityKind.CLASS: "Class",
    EntityKind.DATATYPE: "Datatype",
    EntityKind.OBJECT_PROPERTY: "Object property",
    EntityKind.DATA_PROPERTY: "Data property",
    EntityKind.ANNOTATION_PROPERTY: "Annotation property",
    EntityKind.NAMED_INDIVIDUAL: "Named individual",
}
"""Each kind's heading over a punned IRI's sections for it (`by_kind`), as the page header and the hierarchy column
name the kind (`browl.derived.TYPE_LABELS`)."""

_CLASS_ORDER = (
    Section.EQUIVALENT_TO,
    Section.SUB_CLASS_OF,
    Section.DISJOINT_WITH,
    Section.DISJOINT_UNION_OF,
    Section.KEYS,
    Section.GENERAL,
)
_PROPERTY_ORDER = (
    Section.EQUIVALENT_TO,
    Section.SUB_PROPERTY_OF,
    Section.DOMAINS,
    Section.RANGES,
    Section.CHARACTERISTICS,
    Section.INVERSE_OF,
    Section.DISJOINT_WITH,
    Section.CHAINS,
)
_INDIVIDUAL_ORDER = (
    Section.TYPES,
    Section.SAME_AS,
    Section.DIFFERENT_FROM,
    Section.OBJECT_ASSERTIONS,
    Section.DATA_ASSERTIONS,
    Section.NEGATIVE_ASSERTIONS,
)
_ORDERS = {EntityKind.CLASS: _CLASS_ORDER, EntityKind.NAMED_INDIVIDUAL: _INDIVIDUAL_ORDER}
_SECTION_INDEX = {
    kind: {section: index for index, section in enumerate(_ORDERS.get(kind, _PROPERTY_ORDER))} for kind in EntityKind
}
_BY_PROPERTY = frozenset({Section.OBJECT_ASSERTIONS, Section.DATA_ASSERTIONS})
"""The sections whose lines are under their property's name, each property a group of its own."""
_KIND_INDEX = {kind: index for index, kind in enumerate(EntityKind)}

Operand = Annotated[
    owl.ClassExpression
    | owl.DataRange
    | owl.ObjectPropertyExpression
    | owl.IRIReference
    | owl.AnonymousIndividual
    | owl.Literal,
    Field(discriminator="type"),
]
"""An operand a frame's line shows: a class or property expression, a data range, an annotation property's domain or
range, or an individual or literal an assertion names."""

_DOMAINS = (owl.ObjectPropertyDomain, owl.DataPropertyDomain, owl.AnnotationPropertyDomain)
_RANGES = (owl.ObjectPropertyRange, owl.DataPropertyRange, owl.AnnotationPropertyRange)
_SUB_PROPERTIES = (owl.SubObjectPropertyOf, owl.SubDataPropertyOf, owl.SubAnnotationPropertyOf)
_CHARACTERISTICS = (
    owl.FunctionalObjectProperty,
    owl.InverseFunctionalObjectProperty,
    owl.ReflexiveObjectProperty,
    owl.IrreflexiveObjectProperty,
    owl.SymmetricObjectProperty,
    owl.AsymmetricObjectProperty,
    owl.TransitiveObjectProperty,
    owl.FunctionalDataProperty,
)
_EQUIVALENTS = (owl.EquivalentClasses, owl.EquivalentObjectProperties, owl.EquivalentDataProperties)
_DISJOINTS = (owl.DisjointClasses, owl.DisjointObjectProperties, owl.DisjointDataProperties)
_CLASS_AXIOMS = (owl.SubClassOf, owl.EquivalentClasses, owl.DisjointClasses, owl.DisjointUnion, owl.HasKey)
_NEGATIVE_ASSERTIONS = (owl.NegativeObjectPropertyAssertion, owl.NegativeDataPropertyAssertion)


class FrameLine(BaseModel):
    """A line of a frame's section: an axiom, by the operands that aren't the entity, or whole.

    Attributes:
        axiom: The axiom, without its annotations; None for an equivalent class that only the class's equivalence group
            implies, through a chain of equivalences or a cycle of subclass axioms (DESIGN.md §2.5), which the page
            marks as implied.
        operands: What the line shows of the axiom, in order; empty to show it whole, or a characteristic by its
            keyword. A negative assertion's are its property and object.
        annotations: The annotations of the axioms the line shows, each once (DESIGN.md §2.8, Axiom annotations).
    """

    model_config = ConfigDict(frozen=True)

    axiom: AnyAxiom | None
    operands: tuple[Operand, ...] = ()
    annotations: tuple[owl.Annotation, ...] = ()

    def manchester_tokens(self) -> tuple[Token, ...]:
        """Return the line in Manchester syntax, as tokens with their spacing (`browl.owl.Node.manchester_tokens`).

        Operands are separated by commas, by `o` in a property chain, or by a space in a negative assertion
        (`knows bob`); a characteristic is its keyword, without its colon (`Functional`).
        """
        return self._manchester_tokens

    @functools.cached_property
    def _manchester_tokens(self) -> tuple[Token, ...]:
        """The tokens, made once per line, as a page sorts lines by their text and then renders them."""
        if isinstance(self.axiom, _CHARACTERISTICS):
            return (Token(TokenKind.KEYWORD, self.axiom.keyword.removesuffix(":")),)
        if self.operands or self.axiom is None:
            return owl.manchester_list(
                self.operands,
                chain=isinstance(self.axiom, owl.SubPropertyChainOf),
                spaced=isinstance(self.axiom, _NEGATIVE_ASSERTIONS),
            )
        return self.axiom.manchester_tokens()


class AxiomGroup(BaseModel):
    """A section of the frame, with its lines.

    Attributes:
        section: The section.
        kind: The kind of entity it's a section for; None for `Section.OTHER`. An IRI with several types (punning) has
            sections for each, which the page shows under a heading for each kind (`by_kind`).
        label: The section's label, as the page shows it (`SECTION_LABELS`).
        lines: Its lines, in the order shown (`frame_groups`); none for the Types section of an individual with no
            types, which the page shows anyway, as where a reader looks for them first.
        property: For an individual's object or data property assertions, the property whose values its lines show,
            as a section has a group for each, by the property's name; else None.
    """

    model_config = ConfigDict(frozen=True)

    section: Section
    kind: EntityKind | None
    label: str
    lines: tuple[FrameLine, ...]
    property: Entity | None = None


class KindGroups(BaseModel):
    """A punned IRI's sections for one of its kinds, under the kind's heading; or sections under no heading.

    Attributes:
        kind: The kind; None for sections under no heading: all of an IRI's with one kind, or a punned IRI's Other
            axioms, after its kinds'.
        groups: The sections, in the order shown.
    """

    model_config = ConfigDict(frozen=True)

    kind: EntityKind | None
    groups: tuple[AxiomGroup, ...]

    @property
    def label(self) -> str | None:
        """Return the kind's heading (`KIND_LABELS`), or None for sections under no heading."""
        return None if self.kind is None else KIND_LABELS[self.kind]


def by_kind(groups: Sequence[AxiomGroup]) -> list[KindGroups]:
    """Return a frame's sections (`frame_groups`) under their kinds' headings, if they're for more than one kind.

    A punned IRI's sections for each kind are under its heading, in `frame_groups`' order, so a reader can tell which
    of its types a section is for; its Other axioms, which aren't for one kind, follow, under none.
    """
    kinds = list(dict.fromkeys(group.kind for group in groups if group.kind is not None))
    if len(kinds) < 2:
        return [KindGroups(kind=None, groups=tuple(groups))] if groups else []
    return [
        KindGroups(kind=kind, groups=tuple(group for group in groups if group.kind == kind))
        for kind in [*kinds, None]
        if any(group.kind == kind for group in groups)
    ]


def frame_groups(
    axioms: Iterable[AnyAxiom],
    iri: str,
    equivalents: Iterable[IRI],
    shown_name: Callable[[str], str],
    *,
    individual: bool = False,
) -> list[AxiomGroup]:
    """Return an entity's axioms as a frame's sections, in the order the page shows them (DESIGN.md §2.8).

    Axioms that differ only in their annotations are one line, as are those that state one equivalent; a line holds the
    annotations of each, each once.

    Args:
        axioms: The entity's axioms (`browl.axioms.entity_axioms`), with their annotations.
        iri: The entity's IRI.
        equivalents: The other members of the class's equivalence group, if it's in one (the `equiv_group` table), not
            the entity itself.
        shown_name: The name the page shows for an IRI.
        individual: Whether the entity is a named individual, whose Types section is shown even if it has no types.
    """
    # Each section's lines, by what makes them one: the line without annotations, or under Equivalent To and Same As
    # its equivalent, which several axioms, or the equivalence group, may state; with their annotations. A section by
    # property has a group for each.
    sections: dict[_Key, dict[object, tuple[FrameLine, dict[owl.Annotation, None]]]] = {}
    for axiom in axioms:
        for kind, section, line, by in _place(axiom.without_annotations(), iri):
            same = line.operands[0] if section in (Section.EQUIVALENT_TO, Section.SAME_AS) else line
            _, annotations = sections.setdefault((kind, section, by), {}).setdefault(same, (line, {}))
            annotations.update(dict.fromkeys(axiom.annotations))
    for each in equivalents:
        entity = Entity(kind=EntityKind.CLASS, iri=each)
        sections.setdefault((EntityKind.CLASS, Section.EQUIVALENT_TO, None), {}).setdefault(
            entity, (FrameLine(axiom=None, operands=(entity,)), {})
        )
    if individual:
        sections.setdefault((EntityKind.NAMED_INDIVIDUAL, Section.TYPES, None), {})

    def line_key(line: FrameLine) -> tuple[bool, str, str]:
        tokens = line.manchester_tokens()
        named = len(line.operands) == 1 and isinstance(line.operands[0], Entity)
        return not named, _shown_text(tokens, shown_name).casefold(), owl.manchester_text(tokens)

    return [
        AxiomGroup(
            section=section,
            kind=kind,
            label=SECTION_LABELS[section],
            lines=tuple(
                sorted(
                    (
                        line.model_copy(update={"annotations": tuple(annotations)}) if annotations else line
                        for line, annotations in sections[kind, section, by].values()
                    ),
                    key=line_key,
                )
            ),
            property=by,
        )
        for kind, section, by in sorted(sections, key=lambda key: _section_key(key, shown_name))
    ]


type _Key = tuple[EntityKind | None, Section, Entity | None]
"""A group of a frame's lines: its section's kind, the section, and for a section by property, the property."""


def _place(axiom: AnyAxiom, iri: str) -> Iterator[tuple[EntityKind | None, Section, FrameLine, Entity | None]]:
    """Yield the group (`_Key`) of each line that shows an axiom on the entity's page, and the line."""
    for kind, section, line in _sections(axiom, iri):
        by: Entity | None = None
        if section in _BY_PROPERTY:
            assert isinstance(line.axiom, owl.ObjectPropertyAssertion | owl.DataPropertyAssertion)
            assert isinstance(line.axiom.property, Entity)  # an assertion of an inverse property is shown whole
            by = line.axiom.property
        yield kind, section, line, by


def _sections(axiom: AnyAxiom, iri: str) -> Iterator[tuple[EntityKind | None, Section, FrameLine]]:
    """Yield the section, and its kind, of each line that shows an axiom on the entity's page."""
    if isinstance(axiom, owl.SubClassOf) and _is(axiom.sub, iri):
        yield EntityKind.CLASS, Section.SUB_CLASS_OF, FrameLine(axiom=axiom, operands=(axiom.super,))
    elif isinstance(axiom, _EQUIVALENTS) and (entity := _member(axiom.members, iri)):
        # One member, from `C owl:equivalentClass C`: the entity is its own equivalent.
        for member in [member for member in axiom.members if member is not entity] or [entity]:
            yield entity.kind, Section.EQUIVALENT_TO, FrameLine(axiom=axiom, operands=(member,))
    elif isinstance(axiom, _DISJOINTS) and (entity := _member(axiom.members, iri)):
        # One member, from `C owl:disjointWith C`: the entity is disjoint with itself, so it's empty.
        others = tuple(member for member in axiom.members if member is not entity) or (entity,)
        yield entity.kind, Section.DISJOINT_WITH, FrameLine(axiom=axiom, operands=others)
    elif isinstance(axiom, owl.DisjointUnion) and _is(axiom.class_, iri) and axiom.members:
        yield EntityKind.CLASS, Section.DISJOINT_UNION_OF, FrameLine(axiom=axiom, operands=axiom.members)
    elif (
        isinstance(axiom, owl.HasKey)
        and _is(axiom.class_, iri)
        and (keys := (*axiom.object_properties, *axiom.data_properties))
    ):
        yield EntityKind.CLASS, Section.KEYS, FrameLine(axiom=axiom, operands=keys)
    elif isinstance(axiom, _CLASS_AXIOMS):
        yield EntityKind.CLASS, Section.GENERAL, FrameLine(axiom=axiom)
    elif isinstance(axiom, _DOMAINS) and _is(axiom.property, iri):
        yield axiom.property.kind, Section.DOMAINS, FrameLine(axiom=axiom, operands=(axiom.domain,))
    elif isinstance(axiom, _RANGES) and _is(axiom.property, iri):
        yield axiom.property.kind, Section.RANGES, FrameLine(axiom=axiom, operands=(axiom.range,))
    elif isinstance(axiom, _CHARACTERISTICS) and _is(axiom.property, iri):
        yield axiom.property.kind, Section.CHARACTERISTICS, FrameLine(axiom=axiom)
    elif isinstance(axiom, _SUB_PROPERTIES) and _is(axiom.sub, iri):
        yield axiom.sub.kind, Section.SUB_PROPERTY_OF, FrameLine(axiom=axiom, operands=(axiom.super,))
    elif isinstance(axiom, owl.SubPropertyChainOf) and _is(axiom.super, iri):
        yield axiom.super.kind, Section.CHAINS, FrameLine(axiom=axiom, operands=axiom.chain)
    elif isinstance(axiom, owl.InverseObjectProperties) and (entity := _member((axiom.first, axiom.second), iri)):
        # The other side; or the entity itself, if it's its own inverse (`p owl:inverseOf p`).
        other = axiom.second if entity is axiom.first else axiom.first
        yield entity.kind, Section.INVERSE_OF, FrameLine(axiom=axiom, operands=(other,))
    elif isinstance(axiom, owl.ClassAssertion) and _is(axiom.individual, iri):
        yield EntityKind.NAMED_INDIVIDUAL, Section.TYPES, FrameLine(axiom=axiom, operands=(axiom.class_,))
    elif isinstance(axiom, owl.SameIndividual) and (entity := _member(axiom.members, iri)):
        # One member, from `i owl:sameAs i`: the individual is the same as itself.
        for same in [each for each in axiom.members if each is not entity] or [entity]:
            yield EntityKind.NAMED_INDIVIDUAL, Section.SAME_AS, FrameLine(axiom=axiom, operands=(same,))
    elif isinstance(axiom, owl.DifferentIndividuals) and (entity := _member(axiom.members, iri)):
        different = tuple(each for each in axiom.members if each is not entity) or (entity,)
        yield EntityKind.NAMED_INDIVIDUAL, Section.DIFFERENT_FROM, FrameLine(axiom=axiom, operands=different)
    elif (
        isinstance(axiom, owl.ObjectPropertyAssertion | owl.DataPropertyAssertion)
        and _is(axiom.subject, iri)
        and isinstance(axiom.property, Entity)
    ):
        is_object = isinstance(axiom, owl.ObjectPropertyAssertion)
        section = Section.OBJECT_ASSERTIONS if is_object else Section.DATA_ASSERTIONS
        yield EntityKind.NAMED_INDIVIDUAL, section, FrameLine(axiom=axiom, operands=(axiom.object,))
    elif isinstance(axiom, _NEGATIVE_ASSERTIONS) and _is(axiom.subject, iri):
        line = FrameLine(axiom=axiom, operands=(axiom.property, axiom.object))
        yield EntityKind.NAMED_INDIVIDUAL, Section.NEGATIVE_ASSERTIONS, line
    else:  # such as an inverse property's assertion, written with the entity first
        yield None, Section.OTHER, FrameLine(axiom=axiom.with_first(iri))


def _is(node: owl.Node, iri: str) -> TypeGuard[Entity]:
    """Return whether a node is the entity: a named entity with its IRI, not an expression that mentions it."""
    return isinstance(node, Entity) and node.iri == iri


def _member(members: Sequence[owl.Node], iri: str) -> Entity | None:
    """Return the member of an n-ary axiom, or the side of an inverse axiom, that is the entity, if one is."""
    return next((member for member in members if _is(member, iri)), None)


def _section_key(key: _Key, shown_name: Callable[[str], str]) -> tuple[int, int, str, str]:
    """Order groups by kind, a class's first, then by the kind's order of sections, then by property name and IRI.

    Other axioms come last.
    """
    kind, section, by = key
    if kind is None:
        return len(EntityKind), 0, "", ""
    name, by_iri = ("", "") if by is None else (shown_name(by.iri).casefold(), by.iri)
    return _KIND_INDEX[kind], _SECTION_INDEX[kind][section], name, by_iri


def _shown_text(tokens: Iterable[Token], shown_name: Callable[[str], str]) -> str:
    """Return tokens as text, each IRI by the name the page shows for it, for sorting."""
    return "".join(
        f"{' ' if token.space else ''}{shown_name(token.iri) if token.iri is not None else token.text}"
        for token in tokens
    )
