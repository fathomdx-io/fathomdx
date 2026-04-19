FROM python:3.12-slim

WORKDIR /app

COPY api/requirements.txt /app/api/requirements.txt
RUN pip install --no-cache-dir -r /app/api/requirements.txt

COPY api/ /app/api/
COPY ui/ /app/ui/

EXPOSE 8200
CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8200"]
