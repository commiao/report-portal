# report-portal image — a tiny Starlette/uvicorn aggregator. No DB, no embeddings;
# it only fetches each source's /portal_manifest over HTTP and renders a card grid.
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY portal.py /app/portal.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

CMD ["python", "portal.py"]
