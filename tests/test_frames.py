"""Tests for an entity's axioms as a frame's sections (`browl.frames`)."""

import pytest
from pydantic import TypeAdapter

from browl import owl
from browl.frames import AxiomGroup, FrameLine, Section, by_kind, frame_groups
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
        ("Types", [iri("Organ")]),
    ]
    assert [group.section for group in groups] == [
        Section.EQUIVALENT_TO,
        Section.SUB_CLASS_OF,
        Section.DISJOINT_WITH,
        Section.DISJOINT_UNION_OF,
        Section.KEYS,
        Section.GENERAL,
        Section.TYPES,
    ]
    assert groups[-1].kind == EntityKind.NAMED_INDIVIDUAL
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


def headed(groups: list[AxiomGroup]) -> list[tuple[str | None, list[str]]]:
    """Return sections under their kinds' headings (`by_kind`), as each heading and its sections' labels."""
    return [(block.label, [group.label for group in block.groups]) for block in by_kind(groups)]


def test_punned_labels() -> None:
    """Check that a punned IRI's sections are under a heading for each kind, in order, their labels without it."""
    p = entity("p", EntityKind.OBJECT_PROPERTY)
    d = entity("p", EntityKind.DATA_PROPERTY)
    axioms = [
        owl.EquivalentObjectProperties(members=(p, entity("q", EntityKind.OBJECT_PROPERTY))),
        owl.EquivalentDataProperties(members=(d, entity("e", EntityKind.DATA_PROPERTY))),
        owl.ObjectPropertyDomain(property=p, domain=HEART),
        owl.SubClassOf(sub=entity("p"), super=ORGAN),
    ]
    groups = frame_groups(axioms, p.iri, [], shown_name)
    assert headed(groups) == [
        ("Class", ["SubClass Of"]),
        ("Object property", ["Equivalent To", "Domains"]),
        ("Data property", ["Equivalent To"]),
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
    assert text(groups) == [("Equivalent To", [iri("q")])] * 3
    assert headed(groups) == [(heading, ["Equivalent To"]) for heading in ("Class", "Object property", "Data property")]
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
SELF = entity("i", EntityKind.NAMED_INDIVIDUAL)


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
        (owl.SameIndividual(members=(SELF,)), SELF, [("Same As", [iri("i")])]),
        (owl.DifferentIndividuals(members=(SELF,)), SELF, [("Different From", [iri("i")])]),
    ],
    ids=[
        "self-inverse",
        "inverse-expression",
        "inverse-of-inverse",
        "self-equivalent",
        "self-disjoint",
        "self-equivalent-property",
        "empty-key",
        "self-same",
        "self-different",
    ],
)
def test_edge_cases(axiom: owl.AnyAxiom, page: Entity, expected: list[tuple[str, list[str]]]) -> None:
    """Check where an axiom whose entity is on both sides, or that is empty, goes."""
    assert text(frame_groups([axiom], page.iri, [], shown_name)) == expected


def test_class_and_property_labels() -> None:
    """Check that a punned class's and object property's sections are under a heading for each."""
    axioms = [
        owl.DisjointClasses(members=(entity("p"), HEART)),
        owl.DisjointObjectProperties(members=(P, Q)),
    ]
    assert headed(frame_groups(axioms, P.iri, [], shown_name)) == [
        ("Class", ["Disjoint With"]),
        ("Object property", ["Disjoint With"]),
    ]


def test_object_and_annotation_property() -> None:
    """Check that an object and annotation property's sections are under a heading for each, its other axioms after.

    An inverse property's domain is shown whole, under Other axioms, which no heading is over.
    """
    note = entity("p", EntityKind.ANNOTATION_PROPERTY)
    axioms = [
        owl.AnnotationPropertyRange(property=note, range=owl.IRIReference(iri=f"{NS}Range")),
        owl.ObjectPropertyDomain(property=owl.ObjectInverseOf(property=P), domain=HEART),
        owl.SubObjectPropertyOf(sub=P, super=Q),
    ]
    assert headed(frame_groups(axioms, P.iri, [], shown_name)) == [
        ("Object property", ["SubProperty Of"]),
        ("Annotation property", ["Ranges"]),
        (None, ["Other axioms"]),
    ]


def test_one_kind_unheaded() -> None:
    """Check that an entity with one kind's sections, its Other axioms besides, has them all under no heading."""
    axioms = [
        owl.ObjectPropertyDomain(property=owl.ObjectInverseOf(property=P), domain=HEART),
        owl.SubObjectPropertyOf(sub=P, super=Q),
    ]
    assert headed(frame_groups(axioms, P.iri, [], shown_name)) == [(None, ["SubProperty Of", "Other axioms"])]
    assert by_kind([]) == []


A, Z = entity("A", EntityKind.NAMED_INDIVIDUAL), entity("Z", EntityKind.NAMED_INDIVIDUAL)


@pytest.mark.parametrize(
    ("axiom", "expected"),
    [
        (owl.DisjointClasses(members=(entity("A"), entity("Z"))), [("Disjoint With", [iri("A")])]),
        (owl.EquivalentClasses(members=(entity("A"), entity("Z"))), [("Equivalent To", [iri("A")])]),
        (
            owl.InverseObjectProperties(first=entity("A", P.kind), second=entity("Z", P.kind)),
            [("Inverse Of", [iri("A")])],
        ),
        (owl.SameIndividual(members=(A, Z)), [("Same As", [iri("A")])]),
        (owl.DifferentIndividuals(members=(A, Z)), [("Different From", [iri("A")])]),
    ],
    ids=["disjoint", "equivalent", "inverse", "same", "different"],
)
def test_symmetric_entity_first(axiom: owl.AnyAxiom, expected: list[tuple[str, list[str]]]) -> None:
    """Check that a symmetric axiom between two, written with the other first, shows on Z's page with Z first.

    A frame's section shows only the other.
    """
    assert text(frame_groups([axiom], f"{NS}Z", [], shown_name)) == expected


def test_individual_frame() -> None:
    """Check an individual's sections, in order, with each property's assertions a group of its own, by its name."""
    knows, likes = (entity(name, EntityKind.OBJECT_PROPERTY) for name in ("knows", "likes"))
    age, zip_code = (entity(name, EntityKind.DATA_PROPERTY) for name in ("age", "zeta"))  # zeta is shown as "Aeta"
    b, c = entity("B", EntityKind.NAMED_INDIVIDUAL), entity("C", EntityKind.NAMED_INDIVIDUAL)
    anonymous = owl.AnonymousIndividual(id="_:x")
    forty = owl.Literal(lexical="40", datatype="http://www.w3.org/2001/XMLSchema#integer")
    axioms = [
        owl.ObjectPropertyAssertion(subject=Z, property=likes, object=b),
        owl.ObjectPropertyAssertion(subject=Z, property=knows, object=c),
        owl.ObjectPropertyAssertion(subject=Z, property=knows, object=anonymous),
        owl.DataPropertyAssertion(subject=Z, property=age, object=forty),
        owl.DataPropertyAssertion(subject=Z, property=zip_code, object=forty),
        owl.NegativeObjectPropertyAssertion(subject=Z, property=knows, object=A),
        owl.NegativeDataPropertyAssertion(subject=Z, property=age, object=forty),
        owl.DifferentIndividuals(members=(A, Z, b)),
        owl.SameIndividual(members=(A, Z, b)),
        owl.SameIndividual(members=(Z, b)),  # b again: one line
        owl.ClassAssertion(individual=Z, class_=owl.ObjectSomeValuesFrom(property=HAS_PART, filler=MUSCLE)),
        owl.ClassAssertion(individual=Z, class_=ORGAN),
        owl.ObjectPropertyAssertion(subject=Z, property=owl.ObjectInverseOf(property=knows), object=A),
        owl.SubClassOf(sub=entity("Z"), super=ORGAN),  # punning: Z is a class too
    ]
    groups = frame_groups(axioms, Z.iri, [], shown_name)
    forty_text = "40"
    assert text(groups) == [
        ("SubClass Of", [iri("Organ")]),
        ("Types", [iri("Organ"), f"{iri('hasPart')} some {iri('M')}"]),
        ("Same As", [iri("A"), iri("B")]),
        ("Different From", [f"{iri('A')}, {iri('B')}"]),
        ("Object property assertions", [iri("C"), "_:x"]),
        ("Object property assertions", [iri("B")]),
        ("Data property assertions", [forty_text]),
        ("Data property assertions", [forty_text]),
        ("Negative property assertions", [f"{iri('age')} {forty_text}", f"{iri('knows')} {iri('A')}"]),
        ("Other axioms", [f"{iri('Z')} inverse ({iri('knows')}) {iri('A')}"]),
    ]
    assert [group.property for group in groups[4:8]] == [knows, likes, zip_code, age]
    assert groups[0].kind == EntityKind.CLASS
    assert {group.kind for group in groups[1:-1]} == {EntityKind.NAMED_INDIVIDUAL}
    assert [(heading, labels[:1]) for heading, labels in headed(groups)] == [
        ("Class", ["SubClass Of"]),
        ("Named individual", ["Types"]),
        (None, ["Other axioms"]),
    ]


def test_individual_json() -> None:
    """Check that an individual's sections round-trip through JSON (#162, AC8).

    That includes each property's group, with its property, an assertion's anonymous individual or literal, a negative
    assertion's operands, and an empty Types section.
    """
    knows, age = entity("knows", EntityKind.OBJECT_PROPERTY), entity("age", EntityKind.DATA_PROPERTY)
    forty = owl.Literal(lexical="40", datatype="http://www.w3.org/2001/XMLSchema#integer")
    axioms = [
        owl.ObjectPropertyAssertion(subject=Z, property=knows, object=owl.AnonymousIndividual(id="_:x")),
        owl.DataPropertyAssertion(subject=Z, property=age, object=forty),
        owl.NegativeObjectPropertyAssertion(subject=Z, property=knows, object=A),
        owl.SameIndividual(members=(A, Z)),
    ]
    groups = frame_groups(axioms, Z.iri, [], shown_name, individual=True)
    adapter = TypeAdapter(list[AxiomGroup])
    assert adapter.validate_json(adapter.dump_json(groups)) == groups
    assert [(group.section, group.property) for group in groups] == [
        (Section.TYPES, None),
        (Section.SAME_AS, None),
        (Section.OBJECT_ASSERTIONS, knows),
        (Section.DATA_ASSERTIONS, age),
        (Section.NEGATIVE_ASSERTIONS, None),
    ]


def test_individual_types_always() -> None:
    """Check that an individual's Types section is there, empty, if it has no types, and not for another entity."""
    same = owl.SameIndividual(members=(Z, A))
    [types, same_as] = frame_groups([same], Z.iri, [], shown_name, individual=True)
    assert (types.label, types.kind, types.lines) == ("Types", EntityKind.NAMED_INDIVIDUAL, ())
    assert text([same_as]) == [("Same As", [iri("A")])]
    assert text(frame_groups([], Z.iri, [], shown_name, individual=True)) == [("Types", [])]
    assert frame_groups([], Z.iri, [], shown_name) == []
    some = owl.ClassAssertion(individual=Z, class_=owl.ObjectSomeValuesFrom(property=HAS_PART, filler=MUSCLE))
    assert text(frame_groups([some], Z.iri, [], shown_name, individual=True)) == [
        ("Types", [f"{iri('hasPart')} some {iri('M')}"])
    ]
