# We use a lightweight Python base image
FROM python:3.11-slim

# FFmpeg: video thumbnails and near-duplicate frame sampling call the
# ffmpeg/ffprobe binaries directly (app/media/ffmpeg.py). No recommends:
# they pulled in X11/desktop bits nothing here uses.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Set working directory in the container
WORKDIR /app

# Install Python libraries (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code into the container. scripts/ ships because the CLI
# scripts must run inside it (`docker exec mediaindex-web python
# scripts/...`) — the host cannot reach the database on the stack's internal
# network.
COPY run.py ./
COPY app ./app
COPY scripts ./scripts

# Set environment variables
ENV FLASK_APP=run.py
ENV PYTHONUNBUFFERED=1

EXPOSE 5001

# /robots.txt needs no login and touches nothing but the request pipeline,
# so a 200 means a worker is up and answering. Without this Docker only knew
# whether the gunicorn master was alive, not whether it served anything.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5001/robots.txt', timeout=4)"

# Gunicorn: 2 workers × 4 threads so one large upload doesn't block
# every other request. The worker count is not arbitrary — the psycopg
# pool is sized per process for exactly this shape (app/db_utils.py), and
# the single-instance elections for log rotation and the staging scanner
# both assume a small, fixed set of workers. run.py stays for local dev.
CMD ["gunicorn", "--bind", "0.0.0.0:5001", "--workers", "2", "--threads", "4", \
     "--timeout", "600", "--access-logfile", "-", "run:app"]
