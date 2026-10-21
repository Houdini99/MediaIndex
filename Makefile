# MediaIndex — developer tasks.
#
# The test suite needs a real PostgreSQL server: the app is Postgres-only,
# and the fixtures create a throwaway database per run. `make test` runs
# that container for you and cleans it up.
#
#   make test           run the suite (starts/reuses a throwaway Postgres)
#   make test ARGS=…    pass extra pytest arguments
#   make test-fast      skip the tests that wait out a decoder deadline
#   make db-up/db-down  manage the test database container by hand
#   make lint           ruff (pyflakes-level) + syntax-check the JS

PY              ?= .venv/bin/python
PG_CONTAINER    ?= mediaindex-test-pg
PG_PORT         ?= 5433
PG_PASSWORD     ?= test
PG_IMAGE        ?= postgres:17-alpine
TEST_DATABASE_URL ?= postgresql://postgres:$(PG_PASSWORD)@127.0.0.1:$(PG_PORT)/postgres
ARGS            ?=

.PHONY: test test-fast db-up db-down db-wait lint clean help

help:
	@sed -n 's/^#   //p' $(MAKEFILE_LIST)

## Start the throwaway Postgres, reusing it if it is already up. Bound to
## 127.0.0.1 so it is never reachable off the host.
db-up:
	@if [ -z "$$(docker ps -q -f name=^/$(PG_CONTAINER)$$)" ]; then \
	  if [ -n "$$(docker ps -aq -f name=^/$(PG_CONTAINER)$$)" ]; then \
	    echo "→ starting existing $(PG_CONTAINER)"; docker start $(PG_CONTAINER) >/dev/null; \
	  else \
	    echo "→ creating $(PG_CONTAINER) on 127.0.0.1:$(PG_PORT)"; \
	    docker run -d --rm --name $(PG_CONTAINER) \
	      -p 127.0.0.1:$(PG_PORT):5432 \
	      -e POSTGRES_PASSWORD=$(PG_PASSWORD) $(PG_IMAGE) >/dev/null; \
	  fi; \
	else echo "→ reusing running $(PG_CONTAINER)"; fi
	@$(MAKE) --no-print-directory db-wait

## Block until the server actually accepts connections — `docker run`
## returns long before Postgres is ready, which would fail the first run.
db-wait:
	@for i in $$(seq 1 30); do \
	  if docker exec $(PG_CONTAINER) pg_isready -q -U postgres 2>/dev/null; then exit 0; fi; \
	  sleep 1; \
	done; \
	echo "!! $(PG_CONTAINER) did not become ready in 30s"; exit 1

db-down:
	@docker rm -f $(PG_CONTAINER) >/dev/null 2>&1 || true
	@echo "→ removed $(PG_CONTAINER)"

## The container is left running so repeat runs start instantly; use
## `make db-down` to clean up. The *databases* inside it are per-run
## throwaways that the fixtures drop themselves.
test: db-up
	@TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(PY) -m pytest $(ARGS)

## Skips the tests that wait on a real decoder deadline.
test-fast: db-up
	@TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(PY) -m pytest \
	  --deselect tests/test_ffmpeg.py::test_run_kills_a_hung_process_at_the_deadline \
	  --deselect tests/test_ffmpeg.py::test_a_killed_process_is_reaped \
	  --deselect tests/test_ffmpeg.py::test_thumb_falls_back_to_placeholder_on_a_hang \
	  --deselect tests/test_dupes.py::test_a_hung_decoder_is_killed $(ARGS)

## ruff with pyflakes-level rules only (ruff.toml): unused and undefined
## names, not style. Falls back to a byte-compile when ruff is not
## installed (pip install -r requirements-dev.txt). The JS is only ever
## executed in a browser and nothing else parses it, so it gets at least a
## syntax check.
lint:
	@if $(PY) -m ruff --version >/dev/null 2>&1; then \
	  $(PY) -m ruff check --no-cache . && echo "python: OK (ruff)"; \
	else \
	  $(PY) -m compileall -q app run.py scripts bots_common discord_bot telegram_bot >/dev/null \
	    && echo "python: OK (syntax only — pip install -r requirements-dev.txt for ruff)"; \
	fi
	@if command -v node >/dev/null 2>&1; then \
	  for f in app/static/js/*.js; do node --check "$$f" || exit 1; done; \
	  echo "javascript: OK"; \
	else echo "javascript: skipped (node not installed)"; fi

clean:
	@find . -name __pycache__ -type d -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .pytest_cache
	@echo "→ cleaned"
