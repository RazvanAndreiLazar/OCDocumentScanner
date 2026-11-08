# trixie has the SANE version printer_backend is written for (SCANIMG_VERSION in scanning.py)
FROM python:3.13-slim-trixie

# Everything the Debian host uses to find scanners, so that the container finds the same ones:
# sane-utils brings scanimage and the standard backends (including escl), sane-airscan the
# network scanners that speak eSCL/WSD and libsane-hpaio HP's own backend (with hplip-data
# for its device list). libsane-hpaio adds itself to /etc/sane.d/dll.d.
RUN apt-get update \
    && apt-get install -y --no-install-recommends sane-utils sane-airscan libsane-hpaio hplip-data \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY printer_backend/requirements.txt backend-requirements.txt
COPY web_frontend/requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore -r backend-requirements.txt -r requirements.txt

COPY printer_backend printer_backend
COPY web_frontend web_frontend

# docker-compose.yaml mounts these two; they exist so the image also starts without it.
# Named "app" because sane-utils already creates a group called "scanner".
RUN useradd --uid 1000 --create-home app \
    && mkdir data scans storage \
    && chown app:app data scans storage

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
EXPOSE 8000

# Check if the port is available on restart
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "import os, socket; socket.create_connection((os.environ.get('WEB_HOST', '0.0.0.0'), int(os.environ.get('WEB_PORT', '8000'))), 3).close()"]

CMD ["python", "-m", "web_frontend"]
