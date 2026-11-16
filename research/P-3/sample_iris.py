"""Sample NCBITaxon class IRIs for the page load, from S-4's edges file, plus a few hub classes.

Usage (from the repo root): python research/P-3/sample_iris.py > research/P-3/data/iris.txt
"""

import random
from pathlib import Path

EDGES = Path(__file__).parent.parent / "S-4" / "data" / "ncbitaxon-edges.tsv"

random.seed(3)
with EDGES.open() as f:
    next(f)
    lines = f.readlines()
iris = {line.split("\t")[0].strip("<>") for line in random.sample(lines, 5000)}
# Hubs with many children and references: root, cellular organisms, Bacteria, Eukaryota, Metazoa.
hubs = [f"http://purl.obolibrary.org/obo/NCBITaxon_{n}" for n in (1, 131567, 2, 2759, 33208)]
print("\n".join(hubs + sorted(iris)))
