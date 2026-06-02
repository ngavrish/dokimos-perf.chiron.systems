# Dokimos Performance Report -- task runner.
#
# This is the Python equivalent of `npm run <script>`: declarative task list
# so you don't have to remember the underlying python invocations.
#
# Usage:
#   make            Show this list
#   make dev        Start the SPA at http://localhost:9999
#   make clean      Remove __pycache__ directories

PY ?= python3

.DEFAULT_GOAL := help
.PHONY: help dev start clean

help:  ## Show available targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

dev:  ## Start the SPA at http://localhost:9999 (Ctrl+C to stop)
	$(PY) spa.py

start: dev  ## Alias for `make dev`

clean:  ## Remove __pycache__ directories and *.pyc files
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
