#!/usr/bin/env bash
# Download a pinned NCBITaxon release. Uses S-1's robot.jar and venv (run ../S-1/fetch.sh first).
set -euo pipefail
cd "$(dirname "$0")"
NCBITAXON_VERSION=v2026-11-09
mkdir -p data
if [ ! -f data/ncbitaxon.owl ]; then
  curl -fsSL "https://github.com/obophenotype/ncbitaxon/releases/download/${NCBITAXON_VERSION}/ncbitaxon.owl.gz" \
    | gunzip > data/ncbitaxon.owl
fi
shasum -a 256 data/ncbitaxon.owl
