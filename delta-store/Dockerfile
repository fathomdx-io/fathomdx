FROM python:3.12-slim

# pg_dump for the backup writer. python:3.12-slim is on Debian 13 (trixie),
# which carries postgresql-client-17 in stock repos — no pgdg juggling needed.
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-17 \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    fastapi uvicorn pydantic \
    asyncpg pgvector \
    open-clip-torch Pillow python-multipart httpx numpy

WORKDIR /app
COPY deltas/ /app/deltas/

EXPOSE 8000
CMD ["uvicorn", "deltas.server:app", "--host", "0.0.0.0", "--port", "8000"]
