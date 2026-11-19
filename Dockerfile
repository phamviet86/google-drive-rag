# google-drive-rag: one container = MCP server (Streamable HTTP) + background Drive sync.
FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# LibreOffice (headless) converts legacy .doc/.ppt/.rtf before extraction.
# Tesseract 5 reads scanned pages and images (Vietnamese + English by default).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui \
      tesseract-ocr tesseract-ocr-vie tesseract-ocr-eng \
      fonts-dejavu-core ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && soffice --version \
 && tesseract --version

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install .

# Real OCR pass on a generated Vietnamese line: the build fails if diacritics are lost.
RUN python -m gdrive_rag.ocr

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
 && mkdir -p /data \
 && chown -R app:app /data

ENV GDRAG_DATA_DIR=/data \
    GDRAG_SECRETS_DIR=/secrets \
    GDRAG_LOCAL_ROOT=/local \
    GDRAG_HOST=0.0.0.0 \
    GDRAG_PORT=8765 \
    HOME=/home/app

USER app
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)"
CMD ["gdrive-rag", "serve"]
