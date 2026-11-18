# Running `make` with no target shows the help.
.DEFAULT_GOAL := help

################################################################################
# DEVELOPMENT
################################################################################

.PHONY: install
install:  ## Install dependencies into the Poetry venv, matching poetry.lock
	poetry sync

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
test: $(if $(ROBOT_JAR),,$(ROBOT_JAR_CACHE)) $(if $(JENA_HOME),,$(JENA_STAMP))  ## Run tests with coverage
	poetry run pytest --cov

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
ci: lint test  ## Run the CI checks locally (lint, then test)

################################################################################
# UTILS
################################################################################

# See https://marmelab.com/blog/2016/02/29/auto-documented-makefile.html
.PHONY: help
help:  ## Show help documentation
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
