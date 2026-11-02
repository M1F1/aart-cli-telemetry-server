.PHONY: quality secret-shape-check lint format format-check typecheck tach test coverage mutants image image-smoke deploy smoke

POETRY ?= poetry
RUN = $(POETRY) run

# Every gate, through scripts/quality.py, which CI runs too; the targets below run one each.
quality:
	$(RUN) python scripts/quality.py

# No tracked file carries a literal in the shape of a credential (scripts/secret_shape_check.py).
secret-shape-check:
	$(RUN) python -m scripts.secret_shape_check

lint:
	$(RUN) ruff check .

format:
	$(RUN) ruff format .

format-check:
	$(RUN) ruff format --check .

typecheck:
	$(RUN) mypy aart_cli_telemetry_server deploy tests scripts

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

# The container image and its check, which needs Docker: not part of `quality`, CI runs it apart.
IMAGE ?= aart-cli-telemetry-server:dev
image:
	docker build -t $(IMAGE) .

image-smoke: image
	$(RUN) python scripts/image_smoke.py $(IMAGE)

# A deployment: TARGET is a module in deploy/targets/, ENVIRONMENT its settings. The plan is printed;
# APPLY=1 runs it. `make smoke URL=https://...` checks a deployed server from here.
TARGET ?=
ENVIRONMENT ?=
APPLY ?=
URL ?=
deploy:
	$(RUN) python -m deploy --target $(TARGET) --environment $(ENVIRONMENT) $(if $(APPLY),--apply)

smoke:
	$(RUN) python -m deploy.smoke $(URL)
