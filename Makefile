# Running `make` with no target shows the help.
.DEFAULT_GOAL := help

################################################################################
# DEVELOPMENT
################################################################################

.PHONY: install
install:  ## Install dependencies into the Poetry venv, matching poetry.lock, and the pre-commit git hook
	poetry sync
	poetry run pre-commit install

.PHONY: doctor
doctor:  ## Check the tools development needs, and say what's missing (CONTRIBUTING.md)
	@scripts/doctor.sh

.PHONY: lint
lint:  ## Run all pre-commit hooks on all files
	poetry run pre-commit run --all-files

# robot.jar for the tests marked jvm, which tests/conftest.py uses unless ROBOT_JAR is set (`make test` then doesn't
# download it): the release and checksum the app image pins, read from the Dockerfile. A file target that depends on
# the Dockerfile, so it's downloaded when it's missing or older than the Dockerfile: bumping ROBOT_VERSION gets the new
# release. Any other Dockerfile edit downloads it again too, which is rare enough not to need a recorded version. The
# download goes to a .part file first, so a failed one leaves nothing behind that looks complete.
ROBOT_JAR_CACHE := .cache/robot.jar

# Jena's command-line tools for the tests that load stores (tests/conftest.py uses them unless JENA_HOME is set): the
# release and checksum the app image pins, read from the Dockerfile, downloaded again when it changes, like robot.jar.
# The tools are unpacked into a .part directory first, renamed once complete.
JENA_STAMP := .cache/jena/bin/tdb2.tdbloader

.PHONY: test
test: $(if $(ROBOT_JAR),,$(ROBOT_JAR_CACHE)) $(if $(JENA_HOME),,$(JENA_STAMP)) browser  ## Run tests with coverage
	poetry run pytest --cov

# Playwright's headless Chromium for the tests marked browser, in Playwright's own cache (~/Library/Caches/ms-playwright
# on macOS, ~/.cache/ms-playwright on Linux), at the version the locked Playwright pins; a no-op once it's there. On
# Linux, Chromium also needs system libraries: `poetry run playwright install-deps chromium-headless-shell` (sudo).
.PHONY: browser
browser:  ## Install the headless Chromium the browser tests use, unless already present
	poetry run playwright install --only-shell chromium

.PHONY: robot-jar
robot-jar: $(ROBOT_JAR_CACHE)  ## Download the pinned robot.jar for the tests, unless already present

$(ROBOT_JAR_CACHE): Dockerfile
	mkdir -p $(@D)
	curl -fsSL -o $@.part \
		"https://github.com/ontodev/robot/releases/download/$$(sed -n 's/^ARG ROBOT_VERSION=//p' Dockerfile)/robot.jar"
	echo "$$(sed -n 's/^ARG ROBOT_SHA256=//p' Dockerfile)  $@.part" | shasum -a 256 -c -
	mv $@.part $@

.PHONY: jena
jena: $(JENA_STAMP)  ## Download the pinned Jena tools for the tests, unless already present

$(JENA_STAMP): Dockerfile
	rm -rf .cache/jena .cache/jena.part .cache/jena.tar.gz
	mkdir -p .cache/jena.part
	version=$$(sed -n 's/^ARG JENA_VERSION=//p' Dockerfile); \
	curl -fsSL -o .cache/jena.tar.gz \
		"https://repo1.maven.org/maven2/org/apache/jena/apache-jena/$$version/apache-jena-$$version.tar.gz"
	echo "$$(sed -n 's/^ARG JENA_SHA512=//p' Dockerfile)  .cache/jena.tar.gz" | shasum -a 512 -c -
	tar -xzf .cache/jena.tar.gz -C .cache/jena.part --strip-components=1
	rm .cache/jena.tar.gz
	mv .cache/jena.part .cache/jena
	touch $@

.PHONY: ci
ci: lint check-schema-docs test  ## Run the CI checks locally (lint, the schema docs, then test)

################################################################################
# SCHEMA DOCS
################################################################################

# tbls (https://github.com/k1LoW/tbls) documents the SQLite databases in docs/schema/ (docs/SCHEMA.md). The checksums
# come from the release's checksums-darwin.txt and checksums-linux.txt. The install directory is named for the version,
# so a bump needs no file-time check, and is renamed into place once complete, so a failed download isn't taken as
# installed. tar is bsdtar on macOS, which reads the .zip too.
TBLS_VERSION := 1.96.1
TBLS_SHA256_darwin_amd64 := c744731de4f96517acac234d65175eb9084968abd1a77268d75007b0443a4241
TBLS_SHA256_darwin_arm64 := 99d1d53b02e06635d4d94e2823fecc78b3249e996b1785a199ba1868f9f28a8f
TBLS_SHA256_linux_amd64 := 18285eef0ba7d917214e8bd7dd6a22afe5fea021eed52e7149100a3d10a2af95
TBLS_SHA256_linux_arm64 := f5da6e28b787ca934326870f7b69812c76e097d35a8ae612533108060f76850a
TBLS_OS := $(shell uname -s | tr '[:upper:]' '[:lower:]')
TBLS_ARCH := $(shell uname -m | sed -e 's/^x86_64$$/amd64/' -e 's/^aarch64$$/arm64/')
TBLS_SHA256 := $(TBLS_SHA256_$(TBLS_OS)_$(TBLS_ARCH))
TBLS_ARCHIVE := tbls_v$(TBLS_VERSION)_$(TBLS_OS)_$(TBLS_ARCH).$(if $(filter darwin,$(TBLS_OS)),zip,tar.gz)
TBLS := .cache/tbls/$(TBLS_VERSION)/tbls

# The databases tbls reads; .tbls/*.yml name them.
SCHEMA_DBS := .cache/schema-dbs
SCHEMA_DOCS := derived state

.PHONY: tbls
tbls: $(TBLS)  ## Download the pinned tbls for the schema docs, unless already present

.PHONY: tbls-version
tbls-version:  ## Print the pinned tbls version, for CI's cache key and make doctor
	@echo $(TBLS_VERSION)

$(TBLS):
	@test -n "$(TBLS_SHA256)" || { echo "tbls: no pinned build for $(TBLS_OS) $(TBLS_ARCH)" >&2; exit 1; }
	rm -rf .cache/tbls .cache/tbls.part
	mkdir -p .cache/tbls.part
	curl -fsSL -o .cache/tbls.part/$(TBLS_ARCHIVE) \
		"https://github.com/k1LoW/tbls/releases/download/v$(TBLS_VERSION)/$(TBLS_ARCHIVE)"
	echo "$(TBLS_SHA256)  .cache/tbls.part/$(TBLS_ARCHIVE)" | shasum -a 256 -c -
	tar -xf .cache/tbls.part/$(TBLS_ARCHIVE) -C .cache/tbls.part tbls
	rm .cache/tbls.part/$(TBLS_ARCHIVE)
	mkdir -p .cache/tbls
	mv .cache/tbls.part $(@D)

.PHONY: schema-dbs
schema-dbs:
	poetry run python scripts/schema_dbs.py $(SCHEMA_DBS)

.PHONY: schema-docs
schema-docs: $(TBLS) schema-dbs  ## Regenerate docs/schema/ from the SQLite schemas in code
	for db in $(SCHEMA_DOCS); do $(TBLS) doc --rm-dist --config .tbls/$$db.yml || exit 1; done

.PHONY: check-schema-docs
check-schema-docs: $(TBLS) schema-dbs  ## Check that docs/schema/ matches the code, and describes every table and column
	for db in $(SCHEMA_DOCS); do \
		$(TBLS) lint --config .tbls/$$db.yml && $(TBLS) diff --config .tbls/$$db.yml || exit 1; \
	done

################################################################################
# UTILS
################################################################################

# See https://marmelab.com/blog/2016/02/29/auto-documented-makefile.html
.PHONY: help
help:  ## Show help documentation
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
