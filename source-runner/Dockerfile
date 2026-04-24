FROM python:3.12-slim

WORKDIR /app

# Separate COPY for requirements.txt so code changes don't invalidate
# the pip-install layer. Matches the delta-store Dockerfile pattern.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY server.py source_runner.py ./
COPY sources/ ./sources/

EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
