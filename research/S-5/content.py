"""Compare what OLS4 shows for a class with what browl plans to show (U-26 Usage, U-38 relation trees).

For each sampled class X:
- Usage (U-26), from the OWLAPI: the axioms in which other entities refer to X, grouped by referring entity. An axiom's
  referring entities are its subject when it has one (the named subclass of SubClassOf, the subject of an annotation
  assertion, the named operands of EquivalentClasses or DisjointClasses, the property of a property axiom), otherwise
  every named entity it mentions (U-26). X itself never counts.
- "Related from", from OLS4's API (every page), as a set of entity IRIs.
- Relation-tree children for one property P (U-38): classes C with SubClassOf(C, P some X), or EquivalentClasses(C,
  ... and (P some X) and ...). OLS4's equivalent is hierarchicalChildren minus children (its subclass children).

Usage: research/S-1/.venv/bin/python research/S-5/content.py <file> <ols4-id> <property-IRI|-> <iri-file> [n]
Runs against OLS4's API at http://localhost:8080 with that ontology loaded; prints one JSON line per class, then a
summary line.
"""

import json
import random
import sys
import urllib.request
from collections import Counter
from pathlib import Path
from urllib.parse import quote

import jpype
import jpype.imports

ROBOT_JAR = Path(__file__).parent.parent / "S-1" / "lib" / "robot.jar"
API = "http://localhost:8080/api/v2/ontologies"


def ols4(path: str) -> dict:
    with urllib.request.urlopen(f"{API}/{path}") as r:
        return json.load(r)


def ols4_all(path: str) -> list[dict]:
    sep = "&" if "?" in path else "?"
    first = ols4(f"{path}{sep}size=1000")
    elements = list(first.get("elements", []))
    for page in range(1, first.get("totalPages", 1)):
        elements += ols4(f"{path}{sep}size=1000&page={page}").get("elements", [])
    return elements


def main() -> None:
    file, onto, prop, iri_file = sys.argv[1:5]
    n = int(sys.argv[5]) if len(sys.argv) > 5 else 50
    jpype.startJVM("-Xmx6g", classpath=[str(ROBOT_JAR)], convertStrings=True)
    from java.io import File
    from org.semanticweb.owlapi.apibinding import OWLManager
    from org.semanticweb.owlapi.model import (
        IRI,
        AxiomType,
        OWLAnnotationAssertionAxiom,
        OWLDisjointClassesAxiom,
        OWLEquivalentClassesAxiom,
        OWLObjectIntersectionOf,
        OWLObjectSomeValuesFrom,
        OWLSubClassOfAxiom,
    )
    from org.semanticweb.owlapi.model.parameters import Imports

    manager = OWLManager.createOWLOntologyManager()
    ont = manager.loadOntologyFromOntologyDocument(File(file))
    df = manager.getOWLDataFactory()
    p = df.getOWLObjectProperty(IRI.create(prop)) if prop != "-" else None

    def referrers(ax, x_iri: str) -> set[str]:
        if isinstance(ax, OWLSubClassOfAxiom) and not ax.getSubClass().isAnonymous():
            subjects = [ax.getSubClass().asOWLClass()]
        elif isinstance(ax, (OWLEquivalentClassesAxiom, OWLDisjointClassesAxiom)):
            subjects = [c.asOWLClass() for c in ax.getClassExpressions() if not c.isAnonymous()]
        elif isinstance(ax, OWLAnnotationAssertionAxiom):
            subj = ax.getSubject()
            return {str(subj)} - {x_iri} if subj.isIRI() else set()
        elif ax.isOfType(
            AxiomType.OBJECT_PROPERTY_DOMAIN,
            AxiomType.OBJECT_PROPERTY_RANGE,
            AxiomType.DATA_PROPERTY_DOMAIN,
            AxiomType.SUB_OBJECT_PROPERTY,
            AxiomType.SUB_PROPERTY_CHAIN_OF,
        ):
            subjects = [e for e in ax.getSignature() if e.isOWLObjectProperty() or e.isOWLDataProperty()]
        else:
            subjects = [e for e in ax.getSignature() if not e.isBuiltIn()]
        return {str(e.getIRI()) for e in subjects} - {x_iri}

    def has_some(expr, x) -> bool:
        """Whether expr is (P some X), or an intersection with (P some X) as an operand."""
        if isinstance(expr, OWLObjectSomeValuesFrom):
            return expr.getProperty().equals(p) and expr.getFiller().equals(x)
        if isinstance(expr, OWLObjectIntersectionOf):
            return any(has_some(op, x) for op in expr.getOperands())
        return False

    # Annotation assertions whose value is an IRI (U-26 counts them), which getReferencingAxioms(entity) leaves out.
    by_value: dict[str, list] = {}
    for ax in ont.getAxioms(AxiomType.ANNOTATION_ASSERTION, Imports.INCLUDED):
        if ax.getValue().isIRI():
            by_value.setdefault(str(ax.getValue()), []).append(ax)

    def kind(ax, x) -> str:
        """The axiom's type, with a direct subclass (SubClassOf(C, X)) told apart from X inside an expression."""
        if isinstance(ax, OWLSubClassOfAxiom) and ax.getSuperClass().equals(x):
            return "SubClassOf(child)"
        return str(ax.getAxiomType())

    iris = Path(iri_file).read_text().split()
    sample = iris[:5] + random.Random(55).sample(iris[5:], min(n, len(iris) - 5)) if len(iris) > n + 5 else iris
    totals: Counter = Counter()
    for x_iri in sample:
        x = df.getOWLClass(IRI.create(x_iri))
        axioms = set(ont.getReferencingAxioms(x, Imports.INCLUDED)) | set(by_value.get(x_iri, []))
        usage: dict[str, list] = {}
        for ax in axioms:
            if ax.isOfType(AxiomType.DECLARATION):
                continue
            if isinstance(ax, OWLAnnotationAssertionAxiom) and str(ax.getSubject()) == x_iri:
                continue
            for r in referrers(ax, x_iri):
                usage.setdefault(r, []).append(ax)
        n_axioms = len({a for v in usage.values() for a in v})
        kinds = Counter(str(a.getAxiomType()) for a in {a for v in usage.values() for a in v})
        equiv_only = {r for r, v in usage.items() if all(isinstance(a, OWLEquivalentClassesAxiom) for a in v)}

        enc = quote(quote(x_iri, safe=""), safe="")
        try:
            related = {e["iri"] for e in ols4_all(f"{onto}/classes/{enc}/relatedFrom?lang=en")}
        except urllib.error.HTTPError as e:
            print(json.dumps({"iri": x_iri, "ols4_error": e.code}))
            continue
        row = {
            "iri": x_iri,
            "browl_axioms": n_axioms,
            "browl_entities": len(usage),
            "browl_axiom_types": dict(kinds),
            "ols4_related_from": len(related),
            "both": len(related & usage.keys()),
            "browl_only": len(usage.keys() - related),
            "ols4_only": len(related - usage.keys()),
            "equivalence_only_referrers": len(equiv_only),
            "equivalence_only_in_ols4": len(equiv_only & related),
            # A few browl-only axioms other than direct subclasses, to see what OLS4 leaves out.
            "browl_only_examples": sorted(
                {str(a) for r in usage.keys() - related for a in usage[r] if kind(a, x) != "SubClassOf(child)"}
            )[:4],
            # How each browl-only referrer refers to X: its axioms' types, plus whether X is reached only inside a
            # class expression (not as a named operand), which is what OLS4's "Related from" seems to drop.
            "browl_only_by_type": dict(
                Counter("+".join(sorted({kind(a, x) for a in usage[r]})) for r in usage.keys() - related)
            ),
        }
        if p is not None:
            tree = set()
            for ax in ont.getReferencingAxioms(x, Imports.INCLUDED):
                if isinstance(ax, OWLSubClassOfAxiom) and not ax.getSubClass().isAnonymous():
                    if has_some(ax.getSuperClass(), x):
                        tree.add(str(ax.getSubClass().asOWLClass().getIRI()))
                elif isinstance(ax, OWLEquivalentClassesAxiom):
                    named = [c.asOWLClass() for c in ax.getClassExpressions() if not c.isAnonymous()]
                    for expr in ax.getClassExpressions():
                        if has_some(expr, x):
                            tree.update(str(c.getIRI()) for c in named if not c.equals(x))
            hier = {e["iri"] for e in ols4_all(f"{onto}/classes/{enc}/hierarchicalChildren?lang=en")}
            isa = {e["iri"] for e in ols4_all(f"{onto}/classes/{enc}/children?lang=en")}
            ols4_tree = hier - isa
            row |= {
                "browl_tree_children": len(tree),
                "ols4_tree_children": len(ols4_tree),
                "tree_browl_only": sorted(tree - ols4_tree)[:5],
                "tree_ols4_only": sorted(ols4_tree - tree)[:5],
                "tree_browl_only_n": len(tree - ols4_tree),
                "tree_ols4_only_n": len(ols4_tree - tree),
            }
        print(json.dumps(row), flush=True)
        for k, v in row.items():
            if isinstance(v, int):
                totals[k] += v
            elif k == "browl_only_by_type":
                for t, c in v.items():
                    totals[f"browl_only:{t}"] += c
        totals["classes"] += 1
    print(json.dumps({"summary": dict(totals)}))


if __name__ == "__main__":
    main()
