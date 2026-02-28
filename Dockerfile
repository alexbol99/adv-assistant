FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN pip install --no-cache-dir uv && \
    uv sync --frozen --no-dev && \
    uv pip install --system .

EXPOSE 8080

CMD ["uvicorn", "adv_assistant.main:app", "--host", "0.0.0.0", "--port", "8080"]
