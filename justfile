test:
    uv run pytest

lint:
    uv run ruff check src tests

fmt:
    uv run ruff format src tests

run *ARGS:
    uv run python -m photoflow {{ARGS}}
