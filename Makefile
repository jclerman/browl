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

.PHONY: test
test: $(if $(ROBOT_JAR),,$(ROBOT_JAR_CACHE))  ## Run tests with coverage
	poetry run pytest --cov

.PHONY: robot-jar
robot-jar: $(ROBOT_JAR_CACHE)  ## Download the pinned robot.jar for the tests, unless already present

$(ROBOT_JAR_CACHE): Dockerfile
	mkdir -p $(@D)
	curl -fsSL -o $@.part \
		"https://github.com/ontodev/robot/releases/download/$$(sed -n 's/^ARG ROBOT_VERSION=//p' Dockerfile)/robot.jar"
	echo "$$(sed -n 's/^ARG ROBOT_SHA256=//p' Dockerfile)  $@.part" | shasum -a 256 -c -
	mv $@.part $@

.PHONY: ci
ci: lint test  ## Run the CI checks locally (lint, then test)

################################################################################
# UTILS
################################################################################

# See https://marmelab.com/blog/2016/02/29/auto-documented-makefile.html
.PHONY: help
help:  ## Show help documentation
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
