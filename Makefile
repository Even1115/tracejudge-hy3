.PHONY: install doctor demo test lint format

install:
	pip install -e ".[dev]"

doctor:
	tracejudge doctor

demo:
	tracejudge demo --mock --case faulty

test:
	pytest -q

lint:
	ruff check .
	ruff format --check .

format:
	ruff format .
