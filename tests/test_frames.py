"""Tests for an entity's axioms as a frame's sections (`browl.frames`)."""

import pytest
from pydantic import TypeAdapter

from browl import owl
from browl.frames import AxiomGroup, FrameLine, Section, frame_groups
from browl.owl import Entity, EntityKind

NS = "http://example.org/onto#"


def entity(name: str, kind: EntityKind = EntityKind.CLASS) -> Entity:
    """Return the entity of a name in the test namespace."""
    return Entity(kind=kind, iri=f"{NS}{name}")


def shown_name(iri: str) -> str:
    """Show an IRI by its local name, uppercased for `Z…` names, so sorting by shown names differs from by IRIs."""
    local = iri.removeprefix(NS)
    return local.replace("z", "A") if local.startswith("z") else local


def text(groups: list[AxiomGroup]) -> list[tuple[str, list[str]]]:
    """Return sections as their labels and lines' Manchester text."""
    return [(group.label, [owl.manchester_text(line.manchester_tokens()) for line in group.lines]) for group in groups]


def iri(name: str) -> str:
    """Return a name's IRI in Manchester text."""
    return f"<{NS}{name}>"


HEART, ORGAN, PART, LUNG, LIVER, MUSCLE = (entity(name) for name in ("Heart", "Organ", "part", "Lung", "Liver", "M"))
HAS_PART = entity("hasPart", EntityKind.OBJECT_PROPERTY)


def test_class_frame() -> None:
    """Check a class's sections, in order: named parents before expressions, each axiom by its other operands."""
    some = owl.ObjectSomeValuesFrom(property=HAS_PART, filler=MUSCLE)
    axioms = [
        owl.SubClassOf(sub=HEART, super=some),
        owl.SubClassOf(sub=HEART, super=ORGAN),
        owl.SubClassOf(sub=HEART, super=entity("zebra")),  # shown as "Aebra", so before Organ
        owl.DisjointClasses(members=(HEART, LUNG)),
        owl.DisjointClasses(members=(HEART, LIVER, LUNG)),
        owl.EquivalentClasses(members=(HEART, entity("Cor"), owl.ObjectIntersectionOf(operands=(ORGAN, some)))),
        owl.DisjointUnion(class_=HEART, members=(LIVER, LUNG)),
        owl.HasKey(class_=HEART, object_properties=(HAS_PART,), data_properties=()),
        owl.SubClassOf(sub=some, super=HEART),  # a general class axiom
        owl.ClassAssertion(individual=entity("Heart", EntityKind.NAMED_INDIVIDUAL), class_=ORGAN),  # punning
    ]
    groups = frame_groups(axioms, HEART.iri, [f"{NS}Herz"], shown_name)
    some_text = f"{iri('hasPart')} some {iri('M')}"
    assert text(groups) == [
        ("Equivalent To", [iri("Cor"), iri("Herz"), f"{iri('Organ')} and ({some_text})"]),
        ("SubClass Of", [iri("zebra"), iri("Organ"), some_text]),
        ("Disjoint With", [iri("Lung"), f"{iri('Liver')}, {iri('Lung')}"]),
        ("Disjoint Union Of", [f"{iri('Liver')}, {iri('Lung')}"]),
        ("Keys", [iri("hasPart")]),
        ("General class axioms", [f"{some_text} SubClassOf {iri('Heart')}"]),
        ("Other axioms", [f"{iri('Heart')} Type {iri('Organ')}"]),
    ]
    assert [group.section for group in groups] == [
        Section.EQUIVALENT_TO,
        Section.SUB_CLASS_OF,
        Section.DISJOINT_WITH,
        Section.DISJOINT_UNION_OF,
        Section.KEYS,
        Section.GENERAL,
        Section.OTHER,
    ]
    [cor, herz, _] = groups[0].lines
    assert cor.axiom is axioms[5]
    assert herz.axiom is None  # only the equivalence group implies it


def test_equivalent_once() -> None:
    """Check that an equivalent stated twice, and in the class's equivalence group too, has one line, from an axiom."""
    axioms = [
        owl.EquivalentClasses(members=(HEART, entity("Cor"))),
        owl.EquivalentClasses(members=(entity("Cor"), HEART)),
    ]
    [group] = frame_groups(axioms, HEART.iri, [f"{NS}Cor"], shown_name)
    [line] = group.lines
    assert line.axiom is axioms[0]


def test_property_frame() -> None:
    """Check an object property's sections, in order, with characteristics by keyword and chains joined by `o`."""
    p, q, r = (entity(name, EntityKind.OBJECT_PROPERTY) for name in "pqr")
    axioms = [
        owl.TransitiveObjectProperty(property=p),
        owl.FunctionalObjectProperty(property=p),
        owl.ObjectPropertyRange(property=p, range=LUNG),
        owl.ObjectPropertyDomain(property=p, domain=HEART),
        owl.SubObjectPropertyOf(sub=p, super=q),
        owl.EquivalentObjectProperties(members=(p, r)),
        owl.InverseObjectProperties(first=q, second=p),
        owl.InverseObjectProperties(first=p, second=r),
        owl.DisjointObjectProperties(members=(p, q)),
        owl.SubPropertyChainOf(chain=(q, r), super=p),
        owl.ObjectPropertyDomain(property=owl.ObjectInverseOf(property=p), domain=LUNG),  # p isn't the operand
    ]
    groups = frame_groups(axioms, p.iri, [], shown_name)
    assert text(groups) == [
        ("Equivalent To", [iri("r")]),
        ("SubProperty Of", [iri("q")]),
        ("Domains", [iri("Heart")]),
        ("Ranges", [iri("Lung")]),
        ("Characteristics", ["Functional", "Transitive"]),
        ("Inverse Of", [iri("q"), iri("r")]),
        ("Disjoint With", [iri("q")]),
        ("SuperProperty Of (Chain)", [f"{iri('q')} o {iri('r')}"]),
        ("Other axioms", [f"inverse ({iri('p')}) Domain {iri('Lung')}"]),
    ]
    assert {group.kind for group in groups[:-1]} == {EntityKind.OBJECT_PROPERTY}
    [characteristic, _] = groups[4].lines
    assert characteristic.manchester_tokens()[0].kind == owl.TokenKind.KEYWORD


def test_data_and_annotation_properties() -> None:
    """Check a data property's and an annotation property's domains, ranges and super-properties."""
    d, e = (entity(name, EntityKind.DATA_PROPERTY) for name in "de")
    integer = Entity(kind=EntityKind.DATATYPE, iri="http://www.w3.org/2001/XMLSchema#integer")
    data = [
        owl.DataPropertyDomain(property=d, domain=HEART),
        owl.DataPropertyRange(property=d, range=integer),
        owl.FunctionalDataProperty(property=d),
        owl.SubDataPropertyOf(sub=d, super=e),
        owl.DisjointDataProperties(members=(d, e)),
    ]
    assert text(frame_groups(data, d.iri, [], shown_name)) == [
        ("SubProperty Of", [iri("e")]),
        ("Domains", [iri("Heart")]),
        ("Ranges", ["<http://www.w3.org/2001/XMLSchema#integer>"]),
        ("Characteristics", ["Functional"]),
        ("Disjoint With", [iri("e")]),
    ]
    a, b = (entity(name, EntityKind.ANNOTATION_PROPERTY) for name in "ab")
    annotation = [
        owl.AnnotationPropertyDomain(property=a, domain=owl.IRIReference(iri=HEART.iri)),
        owl.AnnotationPropertyRange(property=a, range=owl.IRIReference(iri=LUNG.iri)),
        owl.SubAnnotationPropertyOf(sub=a, super=b),
    ]
    assert text(frame_groups(annotation, a.iri, [], shown_name)) == [
        ("SubProperty Of", [iri("b")]),
        ("Domains", [iri("Heart")]),
        ("Ranges", [iri("Lung")]),
    ]


def test_punned_labels() -> None:
    """Check that sections of a punned IRI whose labels clash name their kind, and the others don't."""
    p = entity("p", EntityKind.OBJECT_PROPERTY)
    d = entity("p", EntityKind.DATA_PROPERTY)
    axioms = [
        owl.EquivalentObjectProperties(members=(p, entity("q", EntityKind.OBJECT_PROPERTY))),
        owl.EquivalentDataProperties(members=(d, entity("e", EntityKind.DATA_PROPERTY))),
        owl.ObjectPropertyDomain(property=p, domain=HEART),
        owl.SubClassOf(sub=entity("p"), super=ORGAN),
    ]
    groups = frame_groups(axioms, p.iri, [], shown_name)
    assert [group.label for group in groups] == [
        "SubClass Of",
        "Equivalent To (object property)",
        "Domains",
        "Equivalent To (data property)",
    ]


def test_punned_equivalents() -> None:
    """Check that a punned IRI's kinds each show an equivalent they share, and an implied class equivalent too."""
    p, q = entity("p", EntityKind.OBJECT_PROPERTY), entity("q", EntityKind.OBJECT_PROPERTY)
    axioms = [
        owl.EquivalentObjectProperties(members=(p, q)),
        owl.EquivalentDataProperties(
            members=(entity("p", EntityKind.DATA_PROPERTY), entity("q", EntityKind.DATA_PROPERTY))
        ),
    ]
    groups = frame_groups(axioms, p.iri, [q.iri], shown_name)  # q is also an equivalent class, by the group
    assert text(groups) == [
        ("Equivalent To (class)", [iri("q")]),
        ("Equivalent To (object property)", [iri("q")]),
        ("Equivalent To (data property)", [iri("q")]),
    ]
    assert groups[0].lines[0].axiom is None


def test_equivalent_expression_once() -> None:
    """Check that an equivalent expression two axioms state has one line, from the first."""
    both = owl.ObjectIntersectionOf(operands=(ORGAN, MUSCLE))
    axioms = [owl.EquivalentClasses(members=(HEART, both)), owl.EquivalentClasses(members=(HEART, LUNG, both))]
    [group] = frame_groups(axioms, HEART.iri, [], shown_name)
    assert text([group]) == [("Equivalent To", [iri("Lung"), f"{iri('Organ')} and {iri('M')}"])]
    assert group.lines[1].axiom is axioms[0]


def test_order() -> None:
    """Check lines' order: named entities by shown name, case-insensitively; then expressions by their shown text."""
    names = {"Apple": "zz", "Bee": "apple", "Cat": "Banana", "r": "yy", "s": "aa", "aa": "aa"}

    def named(each: str) -> str:
        return names.get(each.removeprefix(NS), each)

    r, s = (entity(name, EntityKind.OBJECT_PROPERTY) for name in "rs")
    axioms = [
        owl.SubClassOf(sub=HEART, super=entity("Apple")),
        owl.SubClassOf(sub=HEART, super=entity("Bee")),
        owl.SubClassOf(sub=HEART, super=entity("Cat")),
        owl.SubClassOf(sub=HEART, super=owl.ObjectSomeValuesFrom(property=r, filler=LUNG)),
        owl.SubClassOf(sub=HEART, super=owl.ObjectSomeValuesFrom(property=s, filler=LUNG)),
        # The same shown text as the last: their order is by IRI.
        owl.SubClassOf(
            sub=HEART, super=owl.ObjectSomeValuesFrom(property=entity("aa", EntityKind.OBJECT_PROPERTY), filler=LUNG)
        ),
    ]
    [group] = frame_groups(axioms, HEART.iri, [], named)
    assert text([group]) == [
        (
            "SubClass Of",
            [
                iri("Bee"),
                iri("Cat"),
                iri("Apple"),
                f"{iri('aa')} some {iri('Lung')}",
                f"{iri('s')} some {iri('Lung')}",
                f"{iri('r')} some {iri('Lung')}",
            ],
        )
    ]


def test_json() -> None:
    """Check that sections round-trip through JSON, operands and axioms with their fields, implied lines too."""
    some = owl.ObjectSomeValuesFrom(property=HAS_PART, filler=MUSCLE)
    groups = frame_groups([owl.SubClassOf(sub=HEART, super=some)], HEART.iri, [f"{NS}Cor"], shown_name)
    adapter = TypeAdapter(list[AxiomGroup])
    assert adapter.validate_json(adapter.dump_json(groups)) == groups
    assert FrameLine.model_validate_json(groups[1].lines[0].model_dump_json()).operands == (some,)


P, Q = entity("p", EntityKind.OBJECT_PROPERTY), entity("q", EntityKind.OBJECT_PROPERTY)


@pytest.mark.parametrize(
    ("axiom", "page", "expected"),
    [
        (owl.InverseObjectProperties(first=P, second=P), P, [("Inverse Of", [iri("p")])]),
        (
            owl.InverseObjectProperties(first=P, second=owl.ObjectInverseOf(property=Q)),
            P,
            [("Inverse Of", [f"inverse ({iri('q')})"])],
        ),
        (
            owl.InverseObjectProperties(first=owl.ObjectInverseOf(property=P), second=Q),
            P,
            [("Other axioms", [f"inverse ({iri('p')}) InverseOf {iri('q')}"])],
        ),
        (owl.EquivalentClasses(members=(HEART,)), HEART, [("Equivalent To", [iri("Heart")])]),
        (owl.DisjointClasses(members=(HEART,)), HEART, [("Disjoint With", [iri("Heart")])]),
        (owl.EquivalentObjectProperties(members=(P,)), P, [("Equivalent To", [iri("p")])]),
        (
            owl.HasKey(class_=HEART, object_properties=(), data_properties=()),
            HEART,
            [("General class axioms", [f"{iri('Heart')} HasKey"])],
        ),
    ],
    ids=[
        "self-inverse",
        "inverse-expression",
        "inverse-of-inverse",
        "self-equivalent",
        "self-disjoint",
        "self-equivalent-property",
        "empty-key",
    ],
)
def test_edge_cases(axiom: owl.AnyAxiom, page: Entity, expected: list[tuple[str, list[str]]]) -> None:
    """Check where an axiom whose entity is on both sides, or that is empty, goes."""
    assert text(frame_groups([axiom], page.iri, [], shown_name)) == expected


def test_class_and_property_labels() -> None:
    """Check that a punned class's and property's clashing labels name their kind."""
    axioms = [
        owl.DisjointClasses(members=(entity("p"), HEART)),
        owl.DisjointObjectProperties(members=(P, Q)),
    ]
    assert [group.label for group in frame_groups(axioms, P.iri, [], shown_name)] == [
        "Disjoint With (class)",
        "Disjoint With (object property)",
    ]
