#!/usr/bin/env bash
# Fetch S-5's inputs. Uberon and NCBITaxon are S-1's and S-3's files (run their fetch.sh first); this adds the pizza
# ontology, whose release (unlike OBO releases) has no relaxed SubClassOf copies of its equivalence definitions.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data
curl -fsSL -o data/pizza.owl https://raw.githubusercontent.com/owlcs/pizza-ontology/master/pizza.owl
base=$(grep -m1 -oE 'xml:base="[^"]+"' data/pizza.owl | sed -E 's/xml:base="(.*)"/\1/')
grep -oE '<owl:Class rdf:about="#[A-Za-z]+"' data/pizza.owl | sed -E "s|.*\"#(.*)\"|$base#\\1|" | sort -u \
  > data/iris-pizza-all.txt
