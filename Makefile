PYTHON ?= python3
PIP ?= $(PYTHON) -m pip

.PHONY: install uninstall test test-verbose run serve init clean dist

## Install runtime + test dependencies and the package itself (editable)
install:
	$(PIP) install -r requirements.txt
	$(PIP) install -e .

## Run the test suite quietly
test:
	$(PYTHON) -m pytest -q

## Run the test suite with verbose output
test-verbose:
	$(PYTHON) -m pytest -vv

## Initialise the data directory and import sample workflows
init:
	openzap init

## Start the web server, webhook endpoints and cron scheduler
serve:
	openzap serve --port 8080

## Run a workflow once from the command line
run:
	openzap run $(WORKFLOW) --input '$(INPUT)'

## Rebuild distribution artifacts
dist:
	$(PIP) install build
	$(PYTHON) -m build

## Remove build and test artifacts
clean:
	rm -rf build/ dist/ *.egg-info .pytest_cache .coverage htmlcov
