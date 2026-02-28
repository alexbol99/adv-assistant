.PHONY: install lint format test run docker-up

install:
	uv sync --all-groups

lint:
	uv run ruff check .

format:
	uv run ruff format .

test:
	uv run pytest

run:
	uv run uvicorn adv_assistant.main:app --reload --host 0.0.0.0 --port 8080

docker-up:
	docker compose up --build
