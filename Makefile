.PHONY: install lint format test test-docker migrate run docker-up

install:
	uv sync --all-groups

lint:
	uv run ruff check .

format:
	uv run ruff format .

test:
	uv run pytest

test-docker:
	docker compose run --rm -v "$$PWD":/app app sh -lc "UV_PROJECT_ENVIRONMENT=/tmp/adv-assistant-venv uv sync --frozen --all-groups && UV_PROJECT_ENVIRONMENT=/tmp/adv-assistant-venv uv run pytest tests"

migrate:
	set -a; [ -f .env ] && . ./.env; set +a; uv run alembic upgrade head

run:
	set -a; [ -f .env ] && . ./.env; set +a; uv run uvicorn adv_assistant.main:app --reload --host 0.0.0.0 --port 8080

docker-up:
	docker compose up --build
