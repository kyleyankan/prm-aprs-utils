# APRS map images: pictures of what is on the air, for e-paper displays and dashboards
FROM python:3.12-slim

# tzdata: the pictures show the local time; the slim image has no time zone database.
RUN pip install --no-cache-dir tzdata \
 && useradd --uid 1000 --create-home aprs \
 && mkdir -p /data && chown aprs:aprs /data

WORKDIR /app
COPY app/ ./

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8080 \
    TZ=America/New_York

USER aprs
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python3", "-c", "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=8); sys.exit(0)"]

CMD ["python3", "server.py"]
