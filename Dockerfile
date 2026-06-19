# Shared image for the Temporal worker and the FastAPI control plane.
FROM python:3.12-slim

WORKDIR /app

# uv for fast, reproducible installs
RUN pip install --no-cache-dir uv

COPY pyproject.toml ./
RUN uv pip install --system --no-cache \
    "temporalio>=1.8.0" "pydantic>=2.0" "fastapi>=0.110" \
    "uvicorn[standard]>=0.29" "sqlalchemy>=2.0" "psycopg[binary]>=3.1"

COPY . .

# Default command is the worker; the api service overrides it in compose.
CMD ["python", "worker.py"]
