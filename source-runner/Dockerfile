FROM python:3.12-slim

WORKDIR /app

# Separate COPY for requirements.txt so code changes don't invalidate
# the pip-install layer. Matches the delta-store Dockerfile pattern.
#
# build-essential is needed transiently because html-to-markdown 3.x
# has pyo3/Rust bindings that compile from source on archs without a
# prebuilt wheel (notably arm64). Installed and purged in one layer
# so the final image stays lean.
COPY requirements.txt /app/requirements.txt
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && pip install --no-cache-dir -r /app/requirements.txt \
 && apt-get purge -y --auto-remove build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY server.py source_runner.py ./
COPY sources/ ./sources/

EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
