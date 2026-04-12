FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir \
    asyncpg httpx pydantic pydantic-settings \
    numpy python-dotenv python-dateutil \
    fastapi uvicorn

# Install AF-SECAPI from GitHub
ARG GITHUB_PAT
RUN if [ -n "$GITHUB_PAT" ]; then \
      pip install --no-cache-dir \
        git+https://${GITHUB_PAT}@github.com/jscheule-609/AF-SECAPI.git; \
    fi

COPY config/ config/
COPY models/ models/
COPY db/ db/
COPY parsers/ parsers/
COPY scoring/ scoring/
COPY state_machines/ state_machines/
COPY pipeline/ pipeline/
COPY output/ output/
COPY scripts/ scripts/
COPY api/ api/

EXPOSE 8004

CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8004"]
