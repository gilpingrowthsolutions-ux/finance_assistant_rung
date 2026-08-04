.PHONY: lint-html
lint-html:  ## Lint templates/index.html for structural HTML bugs
	python3 scripts/lint_html.py

.PHONY: lint
lint: lint-html  ## Run all linters

# ---------------------------------------------------------------------------
# Tests — every suite runs in one command.  All Python suites are isolated
# to an in-memory SQLite DB (RUNG_DB_PATH=:memory:) so they can NEVER touch
# the real rung_finance.db or .env (regression-guarded by
# tests/test_test_isolation.py).
# ---------------------------------------------------------------------------
.PHONY: test
test: test-py test-js  ## Run the full test suite (Python + JS)

.PHONY: test-py
test-py:  ## Run all Python test suites (.venv/bin/python)
	@set -e; for t in tests/test_*.py; do \
		echo "== $$t =="; \
		.venv/bin/python "$$t"; \
		echo; \
	done

.PHONY: test-js
test-js:  ## Run all Node.js test suites
	@set -e; for t in tests/test_*.js; do \
		echo "== $$t =="; \
		node "$$t"; \
		echo; \
	done

.PHONY: ingest
ingest:  ## Run Kroger ingest against scripts/ingest_store_prices.config.example.json
	python3 scripts/ingest_store_prices.py \
		--config scripts/ingest_store_prices.config.example.json

.PHONY: ingest-dry-run
ingest-dry-run:  ## Preview the Kroger ingest without writing to the DB
	python3 scripts/ingest_store_prices.py \
		--config scripts/ingest_store_prices.config.example.json \
		--dry-run

.PHONY: schedule-print
schedule-print:  ## Show the cron / systemd / k8s schedule strings used by deploy/
	@echo 'cron:        15 3,15 * * *'
	@echo 'systemd:     OnCalendar=*-*-* 03:15:00  +  OnCalendar=*-*-* 15:15:00'
	@echo 'k8s CronJob: schedule: "15 3,15 * * *"'

.PHONY: help
help:  ## Show this help
	@grep -Eh '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'
