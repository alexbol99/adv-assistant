.PHONY: install lint format test test-docker migrate run docker-up fix-pth

install:
	uv sync --all-groups
	@$(MAKE) fix-pth

# Python 3.13+ skips .pth files with the macOS UF_HIDDEN flag.
# uv sometimes creates them with this flag set; clear it so editable installs work.
fix-pth:
	@find .venv -name '*.pth' -exec chflags nohidden {} + 2>/dev/null || true

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
	set -a; [ -f .env ] && . ./.env; set +a; uv run uvicorn --app-dir src adv_assistant.main:app --reload --host 0.0.0.0 --port 8080

docker-up:
	docker compose up --build
