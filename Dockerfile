# Invigismart Pro backend - standard library only, so there is no pip install.
#
# The Python process is the whole server: it serves the static site (index.html,
# landing.html, assets/) and the /api/* routes from the same origin. TLS is NOT
# part of this image - every platform below terminates TLS in front of it.
#
#   docker build -t invigismart .
#   docker run -p 8080:8080 -v invigismart-data:/data --env-file .env.production invigismart
#
FROM python:3.12-slim

# PYTHONUNBUFFERED keeps the startup banner and request log in the platform's log
# stream instead of sitting in a block buffer until the process exits.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DB_PATH=/data/invigismart.db \
    DOCUMENTS_DIR=/data/documents

WORKDIR /app

# server/ plus the four pages and assets/ it serves. Nothing else is needed at
# runtime; .dockerignore keeps the local database, uploads and credentials out.
COPY server/ ./server/
COPY assets/ ./assets/
COPY index.html landing.html terms.html privacy.html api-base.js ./

# SQLite database and the uploaded school PDFs. This must be a volume: with the
# database inside the image, every redeploy silently deletes every paid licence
# and every uploaded document.
RUN mkdir -p /data/documents && chmod 700 /data/documents
VOLUME ["/data"]

EXPOSE 8080

# Lets the platform tell "process up" from "server answering". It reads PORT so
# that a platform which assigns its own port (Render uses 10000) still works.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import os,urllib.request,sys; \
port=os.environ.get('PORT','8080'); \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+port+'/api/health', timeout=4).status==200 else 1)"

CMD ["python3", "-u", "server/app.py"]
