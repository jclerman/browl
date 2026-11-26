"""An entity's logical axioms, grouped into a frame's sections, as typed data the axioms panel renders (DESIGN.md §2.8).

A frame shows each axiom by the operands that aren't the entity, under a section named for its kind: on Heart's page,
`Heart SubClassOf Organ` is `Organ` under SubClass Of. An axiom whose entity isn't where its section expects it, such
as a general class axiom, is shown whole.
"""

import functools
from collections import Counter
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
_SECTION_INDEX = {
    kind: {
        section: index for index, section in enumerate(_CLASS_ORDER if kind is EntityKind.CLASS else _PROPERTY_ORDER)
    }
    for kind in EntityKind
}
_KIND_INDEX = {kind: index for index, kind in enumerate(EntityKind)}
_KIND_WORDS = {
    EntityKind.CLASS: "class",
    EntityKind.DATATYPE: "datatype",
    EntityKind.OBJECT_PROPERTY: "object property",
    EntityKind.DATA_PROPERTY: "data property",
    EntityKind.ANNOTATION_PROPERTY: "annotation property",
    EntityKind.NAMED_INDIVIDUAL: "individual",
}

Operand = Annotated[
    owl.ClassExpression | owl.DataRange | owl.ObjectPropertyExpression | owl.IRIReference, Field(discriminator="type")
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
        axiom: The axiom, without its annotations; None for an equivalent class that only the class's equivalence group
            implies, through a chain of equivalences or a cycle of subclass axioms (DESIGN.md §2.5), which the page
            marks as implied.
        operands: What the line shows of the axiom, in order; empty to show it whole, or a characteristic by its
            keyword.
        annotations: The annotations of the axioms the line shows, each once (DESIGN.md §2.8, Axiom annotations).
    """

    model_config = ConfigDict(frozen=True)

    axiom: AnyAxiom | None
    operands: tuple[Operand, ...] = ()
    annotations: tuple[owl.Annotation, ...] = ()

    def manchester_tokens(self) -> tuple[Token, ...]:
        """Return the line in Manchester syntax, as tokens with their spacing (`browl.owl.Node.manchester_tokens`).

        Operands are separated by commas, or by `o` in a property chain; a characteristic is its keyword, without its
        colon (`Functional`).
        """
        return self._manchester_tokens

    @functools.cached_property
    def _manchester_tokens(self) -> tuple[Token, ...]:
        """The tokens, made once per line, as a page sorts lines by their text and then renders them."""
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
    axioms: Iterable[AnyAxiom], iri: str, equivalents: Iterable[IRI], shown_name: Callable[[str], str]
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
    """
    # Each section's lines, by what makes them one: the line without annotations, or under Equivalent To its
    # equivalent, which several axioms, or the equivalence group, may state; with their annotations.
    sections: dict[tuple[EntityKind | None, Section], dict[object, tuple[FrameLine, dict[owl.Annotation, None]]]] = {}
    for axiom in axioms:
        for kind, section, line in _place(axiom.without_annotations(), iri):
            same = line.operands[0] if section is Section.EQUIVALENT_TO else line
            _, annotations = sections.setdefault((kind, section), {}).setdefault(same, (line, {}))
            annotations.update(dict.fromkeys(axiom.annotations))
    for each in equivalents:
        entity = Entity(kind=EntityKind.CLASS, iri=each)
        sections.setdefault((EntityKind.CLASS, Section.EQUIVALENT_TO), {}).setdefault(
            entity, (FrameLine(axiom=None, operands=(entity,)), {})
        )

    def line_key(line: FrameLine) -> tuple[bool, str, str]:
        tokens = line.manchester_tokens()
        named = len(line.operands) == 1 and isinstance(line.operands[0], Entity)
        return not named, _shown_text(tokens, shown_name).casefold(), owl.manchester_text(tokens)

    keys = sorted(sections, key=_section_key)
    labels = [SECTION_LABELS[section] for _, section in keys]
    counts = Counter(labels)
    return [
        AxiomGroup(
            section=section,
            kind=kind,
            label=label if counts[label] == 1 or kind is None else f"{label} ({_KIND_WORDS[kind]})",
            lines=tuple(
                sorted(
                    (
                        line.model_copy(update={"annotations": tuple(annotations)}) if annotations else line
                        for line, annotations in sections[kind, section].values()
                    ),
                    key=line_key,
                )
            ),
        )
        for (kind, section), label in zip(keys, labels, strict=True)
    ]


def _place(axiom: AnyAxiom, iri: str) -> Iterator[tuple[EntityKind | None, Section, FrameLine]]:
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
    else:  # such as an individual's `SameAs`, written with the entity first
        yield None, Section.OTHER, FrameLine(axiom=axiom.with_first(iri))


def _is(node: owl.Node, iri: str) -> TypeGuard[Entity]:
    """Return whether a node is the entity: a named entity with its IRI, not an expression that mentions it."""
    return isinstance(node, Entity) and node.iri == iri


def _member(members: Sequence[owl.Node], iri: str) -> Entity | None:
    """Return the member of an n-ary axiom, or the side of an inverse axiom, that is the entity, if one is."""
    return next((member for member in members if _is(member, iri)), None)


def _section_key(key: tuple[EntityKind | None, Section]) -> tuple[int, int]:
    """Order sections by kind, a class's first, then by the kind's order of sections; other axioms last."""
    kind, section = key
    if kind is None:
        return len(EntityKind), 0
    return _KIND_INDEX[kind], _SECTION_INDEX[kind][section]


def _shown_text(tokens: Iterable[Token], shown_name: Callable[[str], str]) -> str:
    """Return tokens as text, each IRI by the name the page shows for it, for sorting."""
    return "".join(
        f"{' ' if token.space else ''}{shown_name(token.iri) if token.iri is not None else token.text}"
        for token in tokens
    )
