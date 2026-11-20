"""An entity's logical axioms, grouped into a frame's sections, as typed data the axioms panel renders (DESIGN.md §2.8).

A frame shows each axiom by the operands that aren't the entity, under a section named for its kind: on Heart's page,
`Heart SubClassOf Organ` is `Organ` under SubClass Of. An axiom whose entity isn't where its section expects it, such
as a general class axiom, is shown whole.
"""

from collections.abc import Callable, Iterable, Iterator, Sequence
from enum import StrEnum
from typing import Annotated, TypeGuard

from pydantic import BaseModel, ConfigDict, Field

from browl import owl
from browl.owl import AnyAxiom, Entity, EntityKind, Token, TokenKind
from browl.rdf import IRI


class Section(StrEnum):
    """A section of the frame (DESIGN.md §2.8): a class's, a property's, or one for any other axiom."""

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
    Section.OTHER: "Other axioms",
}
"""Each section's label, as the page shows it."""

_CLASS_ORDER = (
    Section.SUB_CLASS_OF,
    Section.EQUIVALENT_TO,
    Section.DISJOINT_WITH,
    Section.DISJOINT_UNION_OF,
    Section.KEYS,
    Section.GENERAL,
)
_PROPERTY_ORDER = (
    Section.DOMAINS,
    Section.RANGES,
    Section.CHARACTERISTICS,
    Section.SUB_PROPERTY_OF,
    Section.EQUIVALENT_TO,
    Section.INVERSE_OF,
    Section.DISJOINT_WITH,
    Section.CHAINS,
)
_KIND_WORDS = {
    EntityKind.CLASS: "class",
    EntityKind.DATATYPE: "datatype",
    EntityKind.OBJECT_PROPERTY: "object property",
    EntityKind.DATA_PROPERTY: "data property",
    EntityKind.ANNOTATION_PROPERTY: "annotation property",
    EntityKind.NAMED_INDIVIDUAL: "individual",
}

Operand = Annotated[
    Entity
    | owl.IRIReference
    | owl.ObjectInverseOf
    | owl.ObjectIntersectionOf
    | owl.ObjectUnionOf
    | owl.ObjectComplementOf
    | owl.ObjectOneOf
    | owl.ObjectSomeValuesFrom
    | owl.ObjectAllValuesFrom
    | owl.ObjectHasValue
    | owl.ObjectHasSelf
    | owl.ObjectMinCardinality
    | owl.ObjectMaxCardinality
    | owl.ObjectExactCardinality
    | owl.DataSomeValuesFrom
    | owl.DataAllValuesFrom
    | owl.DataHasValue
    | owl.DataMinCardinality
    | owl.DataMaxCardinality
    | owl.DataExactCardinality
    | owl.DataIntersectionOf
    | owl.DataUnionOf
    | owl.DataComplementOf
    | owl.DataOneOf
    | owl.DatatypeRestriction,
    Field(discriminator="type"),
]
"""An operand a frame's line shows: a class or property expression, a data range, or an annotation property's domain
or range."""

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


class FrameLine(BaseModel):
    """A line of a frame's section: an axiom, by the operands that aren't the entity, or whole.

    Attributes:
        axiom: The axiom; None for an equivalent class that only the class's equivalence group implies, through a chain
            of equivalences or a cycle of subclass axioms (DESIGN.md §2.5), which the page marks as implied.
        operands: What the line shows of the axiom, in order; empty to show it whole, or a characteristic by its
            keyword.
    """

    model_config = ConfigDict(frozen=True)

    axiom: AnyAxiom | None
    operands: tuple[Operand, ...] = ()

    def manchester_tokens(self) -> tuple[Token, ...]:
        """Return the line in Manchester syntax, as tokens with their spacing (`browl.owl.Node.manchester_tokens`).

        Operands are separated by commas, or by `o` in a property chain; a characteristic is its keyword, without its
        colon (`Functional`).
        """
        if isinstance(self.axiom, _CHARACTERISTICS):
            return (Token(TokenKind.KEYWORD, self.axiom.keyword.removesuffix(":")),)
        if self.operands or self.axiom is None:
            return owl.manchester_list(self.operands, chain=isinstance(self.axiom, owl.SubPropertyChainOf))
        return self.axiom.manchester_tokens()


class AxiomGroup(BaseModel):
    """A section of the frame, with its lines.

    Attributes:
        section: The section.
        kind: The kind of entity it's a section for; None for `Section.OTHER`. An IRI with several types (punning) has
            sections for each.
        label: The section's label, as the page shows it: its `SECTION_LABELS` label, with the kind added if another
            of the entity's sections has that label too ("Equivalent To (object property)").
        lines: Its lines, in the order shown (`frame_groups`).
    """

    model_config = ConfigDict(frozen=True)

    section: Section
    kind: EntityKind | None
    label: str
    lines: tuple[FrameLine, ...]


def frame_groups(
    axioms: Iterable[AnyAxiom], iri: str, equivalents: Iterable[str], shown_name: Callable[[str], str]
) -> list[AxiomGroup]:
    """Return an entity's axioms as a frame's sections, in the order the page shows them (DESIGN.md §2.8).

    A class's sections come first, then a property's, then other axioms; empty ones are left out. Within a section,
    lines that show one named entity come first, by the name the page shows for it, case-insensitively; then the
    others by their text, with the names the page shows. Under Equivalent To, each equivalent is a line of its own,
    once, and so is each member of the class's equivalence group that no axiom states.

    Args:
        axioms: The entity's axioms (`browl.axioms.entity_axioms`).
        iri: The entity's IRI.
        equivalents: The other members of the class's equivalence group, if it's in one (the `equiv_group` table).
        shown_name: The name the page shows for an IRI.
    """
    sections: dict[tuple[EntityKind | None, Section], dict[FrameLine, None]] = {}
    stated: set[str] = set()
    for axiom in axioms:
        for kind, section, line in _place(axiom, iri):
            if section is Section.EQUIVALENT_TO and isinstance(entity := line.operands[0], Entity):
                if entity.iri in stated:
                    continue
                stated.add(entity.iri)
            sections.setdefault((kind, section), {})[line] = None
    for each in equivalents:
        if each not in stated and each != iri:
            entity = Entity(kind=EntityKind.CLASS, iri=IRI(each))
            sections.setdefault((EntityKind.CLASS, Section.EQUIVALENT_TO), {})[
                FrameLine(axiom=None, operands=(entity,))
            ] = None

    def line_key(line: FrameLine) -> tuple[bool, str, str]:
        tokens = line.manchester_tokens()
        named = len(line.operands) == 1 and isinstance(line.operands[0], Entity)
        return not named, _shown_text(tokens, shown_name).casefold(), owl.manchester_text(tokens)

    keys = sorted(sections, key=_section_key)
    labels = [SECTION_LABELS[section] for _, section in keys]
    return [
        AxiomGroup(
            section=section,
            kind=kind,
            label=label if labels.count(label) == 1 or kind is None else f"{label} ({_KIND_WORDS[kind]})",
            lines=tuple(sorted(sections[kind, section], key=line_key)),
        )
        for (kind, section), label in zip(keys, labels, strict=True)
    ]


def _place(axiom: AnyAxiom, iri: str) -> Iterator[tuple[EntityKind | None, Section, FrameLine]]:
    """Yield the section, and its kind, of each line that shows an axiom on the entity's page."""
    if isinstance(axiom, owl.SubClassOf) and _is(axiom.sub, iri):
        yield EntityKind.CLASS, Section.SUB_CLASS_OF, FrameLine(axiom=axiom, operands=(axiom.super,))
    elif isinstance(axiom, _EQUIVALENTS) and (entity := _member(axiom.members, iri)) and len(axiom.members) > 1:
        for member in axiom.members:
            if member is not entity:
                yield entity.kind, Section.EQUIVALENT_TO, FrameLine(axiom=axiom, operands=(member,))
    elif isinstance(axiom, _DISJOINTS) and (entity := _member(axiom.members, iri)) and len(axiom.members) > 1:
        others = tuple(member for member in axiom.members if member is not entity)
        yield entity.kind, Section.DISJOINT_WITH, FrameLine(axiom=axiom, operands=others)
    elif isinstance(axiom, owl.DisjointUnion) and _is(axiom.class_, iri):
        yield EntityKind.CLASS, Section.DISJOINT_UNION_OF, FrameLine(axiom=axiom, operands=axiom.members)
    elif isinstance(axiom, owl.HasKey) and _is(axiom.class_, iri):
        keys = (*axiom.object_properties, *axiom.data_properties)
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
    elif isinstance(axiom, owl.InverseObjectProperties) and _is(axiom.first, iri) and not _is(axiom.second, iri):
        yield axiom.first.kind, Section.INVERSE_OF, FrameLine(axiom=axiom, operands=(axiom.second,))
    elif isinstance(axiom, owl.InverseObjectProperties) and _is(axiom.second, iri) and not _is(axiom.first, iri):
        yield axiom.second.kind, Section.INVERSE_OF, FrameLine(axiom=axiom, operands=(axiom.first,))
    else:
        yield None, Section.OTHER, FrameLine(axiom=axiom)


def _is(node: owl.Node, iri: str) -> TypeGuard[Entity]:
    """Return whether a node is the entity: a named entity with its IRI, not an expression that mentions it."""
    return isinstance(node, Entity) and node.iri == iri


def _member(members: Sequence[owl.Node], iri: str) -> Entity | None:
    """Return the member of an n-ary axiom that is the entity, if one is."""
    return next((member for member in members if isinstance(member, Entity) and member.iri == iri), None)


def _section_key(key: tuple[EntityKind | None, Section]) -> tuple[int, int]:
    """Order sections by kind, a class's first, then by the kind's order of sections; other axioms last."""
    kind, section = key
    if kind is None:
        return len(EntityKind), 0
    order = _CLASS_ORDER if kind is EntityKind.CLASS else _PROPERTY_ORDER
    return list(EntityKind).index(kind), order.index(section)


def _shown_text(tokens: Iterable[Token], shown_name: Callable[[str], str]) -> str:
    """Return tokens as text, each IRI by the name the page shows for it, for sorting."""
    return "".join(
        f"{' ' if token.space else ''}{shown_name(token.iri) if token.iri is not None else token.text}"
        for token in tokens
    )
