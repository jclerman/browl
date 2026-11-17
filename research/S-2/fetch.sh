#!/usr/bin/env bash
# Get the spike's inputs: robot.jar (the version browl's Dockerfile pins), ReasonReuse compiled against it, and the
# generated test ontologies. Needs Java 21.
set -euo pipefail
cd "$(dirname "$0")"
ROBOT_VERSION=v1.9.11
mkdir -p lib/classes data
[ -f lib/robot.jar ] || curl -fsSL -o lib/robot.jar \
  "https://github.com/ontodev/robot/releases/download/${ROBOT_VERSION}/robot.jar"
echo "53cd275c8e4c0dad1fc2c1c34174876795cd7d6b6926e1444adcbb0cb3c7ab26  lib/robot.jar" | shasum -a 256 -c -
javac -cp lib/robot.jar -d lib/classes ReasonReuse.java
python3 gen.py
