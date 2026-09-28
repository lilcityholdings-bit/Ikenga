FROM python:3.12-slim
WORKDIR /app
COPY *.py ./
COPY static ./static
RUN useradd --create-home ladder && mkdir -p /data && chown ladder /data
USER ladder
ENV PORT=8080 DB_PATH=/data/ladder.db
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health')"
CMD ["python", "server.py"]
