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

.PHONY: test
test:  ## Run tests with coverage
	poetry run pytest --cov

.PHONY: ci
ci: lint test  ## Run the CI checks locally (lint, then test)

################################################################################
# UTILS
################################################################################

# See https://marmelab.com/blog/2016/02/29/auto-documented-makefile.html
.PHONY: help
help:  ## Show help documentation
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
