# Layer 05 — the same image in staging and production. Reproducible by digest.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

RUN adduser --system --group --no-create-home app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY api ./api
COPY migrations ./migrations
COPY build_static.py ./
COPY public ./public
COPY ops ./ops

RUN mkdir -p /srv/data && chown -R app:app /srv
USER app

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=4s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/readyz',timeout=3).status==200 else 1)"

CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
