"""Write S-2's test ontologies to data/: one small ontology per failure kind, plus generated stress cases.

python gen.py [--chain N] [--php N] [--wide N]
"""

import argparse
from pathlib import Path

DATA = Path(__file__).parent / "data"
NS = "http://example.org/s2/"
HEADER = f"Prefix(:=<{NS}>)\nPrefix(owl:=<http://www.w3.org/2002/07/owl#>)\n\nOntology(<{NS}{{name}}>\n"


def write(name, axioms):
    path = DATA / f"{name}.ofn"
    with path.open("w") as f:
        f.write(HEADER.format(name=name))
        for axiom in axioms:
            f.write(f"  {axiom}\n")
        f.write(")\n")
    print(f"{path.name}: {path.stat().st_size:,} bytes")


def ok():
    # Classifies fine with both reasoners: Dog is inferred to be a Pet (an Animal that some Person owns).
    yield "Declaration(Class(:Animal))"
    yield "Declaration(Class(:Pet))"
    yield "Declaration(Class(:Dog))"
    yield "Declaration(Class(:Person))"
    yield "Declaration(ObjectProperty(:ownedBy))"
    yield "EquivalentClasses(:Pet ObjectIntersectionOf(:Animal ObjectSomeValuesFrom(:ownedBy :Person)))"
    yield "SubClassOf(:Dog :Animal)"
    yield "SubClassOf(:Dog ObjectSomeValuesFrom(:ownedBy :Person))"


def inconsistent():
    # An individual in two disjoint classes. Inconsistent for ELK too (both constructs are in OWL 2 EL).
    yield "Declaration(Class(:Plant))"
    yield "Declaration(Class(:Animal))"
    yield "Declaration(NamedIndividual(:x))"
    yield "DisjointClasses(:Plant :Animal)"
    yield "ClassAssertion(:Plant :x)"
    yield "ClassAssertion(:Animal :x)"


def unsatisfiable():
    # A class under two disjoint classes, with no individuals: consistent, but Chimera is unsatisfiable.
    yield "Declaration(Class(:Plant))"
    yield "Declaration(Class(:Animal))"
    yield "Declaration(Class(:Chimera))"
    yield "DisjointClasses(:Plant :Animal)"
    yield "SubClassOf(:Chimera :Plant)"
    yield "SubClassOf(:Chimera :Animal)"


def chain(n):
    # C0 ⊑ C1 ⊑ … ⊑ Cn: n²/2 subsumptions, which a reasoner holds in memory while classifying.
    for i in range(n + 1):
        yield f"Declaration(Class(:C{i}))"
    for i in range(n):
        yield f"SubClassOf(:C{i} :C{i + 1})"


def wide(n):
    # n classes, each with an existential to the next and a defined class over it: memory for ELK's saturation
    # grows with n, but only linearly, unlike the chain.
    for i in range(n + 1):
        yield f"Declaration(Class(:W{i}))"
        yield f"Declaration(Class(:D{i}))"
    yield "Declaration(ObjectProperty(:r))"
    for i in range(n):
        yield f"SubClassOf(:W{i} ObjectSomeValuesFrom(:r :W{i + 1}))"
        yield f"EquivalentClasses(:D{i} ObjectSomeValuesFrom(:r :W{i + 1}))"


def pigeonhole(n):
    # n+1 pigeons, n holes: Pigeons is unsatisfiable, but proving it takes a tableau reasoner exponential time in n,
    # in little memory. Uses disjunction, so it's outside OWL 2 EL (ELK ignores the union).
    pigeons = range(n + 1)
    holes = range(n)
    yield "Declaration(Class(:Pigeons))"
    for i in pigeons:
        for j in holes:
            yield f"Declaration(Class(:P{i}_{j}))"
    for i in pigeons:
        yield f"SubClassOf(:Pigeons ObjectUnionOf({' '.join(f':P{i}_{j}' for j in holes)}))"
    for j in holes:
        for i in pigeons:
            for k in pigeons:
                if i < k:
                    yield f"DisjointClasses(:P{i}_{j} :P{k}_{j})"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chain", type=int, default=40_000)
    parser.add_argument("--php", type=int, default=12)
    parser.add_argument("--wide", type=int, default=300_000)
    args = parser.parse_args()
    DATA.mkdir(exist_ok=True)
    write("ok", ok())
    write("inconsistent", inconsistent())
    write("unsatisfiable", unsatisfiable())
    write(f"chain-{args.chain}", chain(args.chain))
    write(f"wide-{args.wide}", wide(args.wide))
    write(f"php-{args.php}", pigeonhole(args.php))
    # Not Turtle: a syntax error in every parser ROBOT tries.
    (DATA / "parse-error.ttl").write_text(f"@prefix : <{NS}> .\n:a :b :c ;; ] .\n")
    print("parse-error.ttl")


if __name__ == "__main__":
    main()
