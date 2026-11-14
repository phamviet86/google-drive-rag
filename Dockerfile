# google-drive-rag: one container = MCP server (Streamable HTTP) + background Drive sync.
FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# LibreOffice (headless) converts legacy .doc/.ppt/.rtf before extraction.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui \
      fonts-dejavu-core ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && soffice --version

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install .

# Pinned OCR runtime (PDFium + ONNX Runtime) and PP-OCR models for this image's architecture.
# provision_and_verify runs a real OCR pass, so a missing library fails the build here.
RUN python -m gdrive_rag.ocr_runtime /opt/ocr \
 && rm -rf /opt/ocr/runtime/downloads

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
 && mkdir -p /data \
 && chown -R app:app /data /opt/ocr

ENV GDRAG_DATA_DIR=/data \
    GDRAG_OCR_DIR=/opt/ocr \
    GDRAG_HOST=0.0.0.0 \
    GDRAG_PORT=8765 \
    HOME=/home/app

USER app
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)"
CMD ["gdrive-rag", "serve"]
