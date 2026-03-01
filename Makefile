.PHONY: install lint format test test-docker run docker-up

install:
	uv sync --all-groups

lint:
	uv run ruff check .

format:
	uv run ruff format .

test:
	uv run pytest

test-docker:
	docker compose run --rm -v "$$PWD":/app app sh -lc "pip install -e . pytest && pytest tests"

run:
	set -a; [ -f .env ] && . ./.env; set +a; uv run uvicorn adv_assistant.main:app --reload --host 0.0.0.0 --port 8080

docker-up:
	docker compose up --build
