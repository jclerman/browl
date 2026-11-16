"""Sample Uberon class IRIs for the page load: 5 hubs plus 5,000 at random from the classes declared in the file.

Usage (from the repo root): python3 research/S-5/sample_iris.py > research/S-5/data/iris-uberon.txt
NCBITaxon uses P-3's sample, research/P-3/data/iris.txt.
"""

import random
import re
from pathlib import Path

FILE = Path(__file__).parent.parent / "S-1" / "data" / "downloads" / "uberon.owl"
pattern = re.compile(r'<owl:Class rdf:about="(http://purl\.obolibrary\.org/obo/UBERON_\d+)"')
classes = sorted(set(pattern.findall(FILE.read_text())))
random.seed(5)
# Hubs: anatomical entity, anatomical structure, multicellular organism, heart, lung.
hubs = [f"http://purl.obolibrary.org/obo/UBERON_{n}" for n in ("0001062", "0000061", "0000468", "0000948", "0002048")]
print("\n".join(hubs + sorted(set(random.sample(classes, 5000)) - set(hubs))))
