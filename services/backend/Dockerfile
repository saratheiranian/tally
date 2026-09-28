# Build from the repo root:  docker build -f services/backend/Dockerfile .
# (the root context lets the image install the local tally-sketches package)
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY packages/sketches /tmp/sketches
COPY services/backend/requirements.txt .
RUN pip install --no-cache-dir /tmp/sketches -r requirements.txt && rm -rf /tmp/sketches

COPY services/backend/app ./app

# Run as non-root.
RUN useradd --create-home --uid 10001 tally
USER tally

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"
# Default is the API; workers override the command with `python -m app.worker`.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
