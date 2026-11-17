# The tracker's server on a private machine (README: "On the private server"). Only the server's packages; the
# code is mounted read-only from /opt/tracker/app, so `deploy` is a copy and a restart, not a rebuild.
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends tzdata curl && rm -rf /var/lib/apt/lists/*
COPY requirements-server.txt /tmp/requirements-server.txt
RUN pip install --no-cache-dir -r /tmp/requirements-server.txt
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER 1000:1000
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s CMD curl -fsS -o /dev/null http://127.0.0.1:8791/api/status || exit 1
CMD ["python", "-m", "tracker", "web", "--no-open"]
