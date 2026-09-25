# Stdlib-only Python service; `anthropic` is installed so ABS_OPTIMIZER=claude works out of the box,
# but nothing imports it unless that optimizer is selected.
FROM python:3.12-slim
RUN pip install --no-cache-dir anthropic \
    && useradd --system --create-home --home-dir /app abs
WORKDIR /app
COPY server.py optimizer.py openapi.yaml ./
COPY entrypoint.sh ./
# State lives in SQLite at /app/data/autobodyshop.db — mount a volume here to keep it. No
# `USER abs`: entrypoint.sh fixes the volume's ownership at start, then drops to `abs`.
RUN chmod +x entrypoint.sh && mkdir -p /app/data
ENV ABS_PORT=8090 ABS_DB=/app/data/autobodyshop.db
EXPOSE 8090
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python3", "-c", "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ['ABS_PORT']+'/health', timeout=2)"]
ENTRYPOINT ["/app/entrypoint.sh"]
