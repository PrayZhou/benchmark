.PHONY: help install lock lint format typecheck test oracle check

help:
	@echo "install    sync the dev environment from uv.lock"
	@echo "lock       refresh uv.lock after changing dependencies"
	@echo "lint       run ruff"
	@echo "format     apply ruff formatting"
	@echo "typecheck  run mypy"
	@echo "test       run pytest"
	@echo "oracle     run the offline task-set self-consistency gate"
	@echo "check      lint + typecheck + test + oracle (what CI runs)"

install:
	uv sync --extra dev

lock:
	uv lock

lint:
	uv run ruff check src tests

format:
	uv run ruff format src tests

typecheck:
	uv run mypy

test:
	uv run pytest

oracle:
	uv run python -m benchmark oracle --tasks-root tasks

check: lint typecheck test oracle
