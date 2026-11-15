#!/usr/bin/env bash
# Download Apache Jena + Fuseki into lib/ (they need Java 21: export JAVA_HOME=$(/usr/libexec/java_home -v 21)). Uses S-1's and S-3's data and robot.jar.
set -euo pipefail
cd "$(dirname "$0")"
JENA_VERSION=6.2.0
mkdir -p lib data
for pkg in apache-jena apache-jena-fuseki; do
  [ -d "lib/${pkg}-${JENA_VERSION}" ] || curl -fsSL "https://dlcdn.apache.org/jena/binaries/${pkg}-${JENA_VERSION}.tar.gz" \
    | tar -xz -C lib
done
ls lib
