# ─────────────────────────────────────────────
#  TMT Translator — Production Dockerfile
#  Supports: PDF, DOCX, CSV/TSV translation
#  Languages: English ↔ Nepali ↔ Tamang
# ─────────────────────────────────────────────
FROM python:3.12-slim

LABEL maintainer="LowResource Labs"
LABEL description="Layout-preserving multilingual document translator (EN/NE/TMG)"
LABEL version="2.0.0"

# ── System dependencies (WeasyPrint + Devanagari fonts) ──────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-freefont-ttf \
    fonts-noto \
    fonts-noto-core \
    fonts-noto-extra \
    curl \
    && fc-cache -f \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies (cached layer) ───────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application source ────────────────────────────────────────────────────────
COPY app.py .
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# ── Runtime directories ───────────────────────────────────────────────────────
RUN mkdir -p /tmp/tmt_uploads /tmp/tmt_outputs

# ── Non-root user for security ────────────────────────────────────────────────
RUN useradd -m -u 1001 tmt && chown -R tmt:tmt /app /tmp/tmt_uploads /tmp/tmt_outputs
USER tmt

# ── Health check ──────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:5050/health || exit 1

EXPOSE 5050

CMD ["python", "app.py", "--host", "0.0.0.0", "--port", "5050"]
