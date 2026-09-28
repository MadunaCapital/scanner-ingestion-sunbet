FROM python:3.12-slim

WORKDIR /app

# git is required at build time: requirements.txt installs
# maduna-scanner-schemas and maduna-scanner-ingestion directly from their
# GitHub repos, not PyPI.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir -r requirements.txt

CMD ["python", "-m", "sunbet"]
