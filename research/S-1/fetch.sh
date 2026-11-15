#!/usr/bin/env bash
# Download the spike's inputs: robot.jar and a pinned Uberon release.
set -euo pipefail
cd "$(dirname "$0")"
ROBOT_VERSION=v1.9.11
UBERON_VERSION=2026-11-05
mkdir -p lib data/downloads
[ -f lib/robot.jar ] || curl -fsSL -o lib/robot.jar \
  "https://github.com/ontodev/robot/releases/download/${ROBOT_VERSION}/robot.jar"
[ -f data/downloads/uberon.owl ] || curl -fsSL -o data/downloads/uberon.owl \
  "http://purl.obolibrary.org/obo/uberon/releases/${UBERON_VERSION}/uberon.owl"
shasum -a 256 lib/robot.jar data/downloads/uberon.owl
