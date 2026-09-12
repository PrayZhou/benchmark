.PHONY: help install lock lint format typecheck test check

help:
	@echo "install    sync the dev environment from uv.lock"
	@echo "lock       refresh uv.lock after changing dependencies"
	@echo "lint       run ruff"
	@echo "format     apply ruff formatting"
	@echo "typecheck  run mypy"
	@echo "test       run pytest"
	@echo "check      lint + typecheck + test (what CI runs)"

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

check: lint typecheck test
