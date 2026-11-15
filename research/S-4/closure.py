"""Write the rdfs:subClassOf closure (each class with every ancestor) as N-Triples, from an edges TSV."""

import sys
import time

t = time.perf_counter()
parent: dict[str, list[str]] = {}
with open(sys.argv[1]) as f:
    next(f)
    for line in f:
        c, p = line.rstrip("\n").split("\t")
        if c.startswith("<") and p.startswith("<"):
            parent.setdefault(c, []).append(p)
anc: dict[str, set[str]] = {}


def ancestors(c: str) -> set[str]:
    stack = [c]
    while stack:  # iterative: NCBITaxon is deep
        n = stack[-1]
        if n in anc:
            stack.pop()
            continue
        pending = [p for p in parent.get(n, []) if p not in anc]
        if pending:
            stack.extend(pending)
            continue
        s: set[str] = set()
        for p in parent.get(n, []):
            s.add(p)
            s |= anc[p]
        anc[n] = s
        stack.pop()
    return anc[c]


n = 0
sc = "<http://www.w3.org/2000/01/rdf-schema#subClassOf>"
with open(sys.argv[2], "w") as out:
    for c in parent:
        for a in ancestors(c):
            out.write(f"{c} {sc} {a} .\n")
            n += 1
print(f"closure triples: {n:,} in {time.perf_counter() - t:.0f}s")
