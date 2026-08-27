FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    ENVIRONMENT=container \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-dev --no-install-project

COPY main.py ./main.py
COPY src ./src
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x ./docker-entrypoint.sh

# RUN_MODE selects the subcommand at container start: "concat" or "sync".
# Left unset on purpose so a misconfigured deploy fails loudly instead of
# silently picking a default. Set it per Cloud Run job:
#   job A -> RUN_MODE=concat   job B -> RUN_MODE=sync
ENTRYPOINT ["/app/docker-entrypoint.sh"]
