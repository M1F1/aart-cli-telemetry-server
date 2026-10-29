.PHONY: quality lint format format-check typecheck tach test coverage mutants

POETRY ?= poetry
RUN = $(POETRY) run

quality: lint format-check typecheck tach coverage

lint:
	$(RUN) ruff check .

format:
	$(RUN) ruff format .

format-check:
	$(RUN) ruff format --check .

typecheck:
	$(RUN) mypy aart_cli_telemetry_server tests

tach:
	$(RUN) tach check
	$(RUN) tach check-external

test:
	$(RUN) pytest -q

coverage:
	$(RUN) coverage run -m pytest -q
	$(RUN) coverage report

# Advisory, not a gate, as in aart-cli. `paths_to_mutate` in pyproject.toml is the scope; ONLY
# narrows the run to matching mutants: `make mutants ONLY='aart_cli_telemetry_server.domain.stats*'`.
ONLY ?=
mutants:
	$(RUN) mutmut run $(ONLY)
	$(RUN) mutmut results
