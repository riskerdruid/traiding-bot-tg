FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

WORKDIR /app

# Зависимости отдельным слоем — пересобираются только при их изменении
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY tools ./tools
COPY tests ./tests
COPY run.py .

# База и логи живут в томах, чтобы переживать пересборку образа
RUN mkdir -p /app/data /app/logs

# Бот сам говорит, здоров ли он
HEALTHCHECK --interval=60s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=5).status==200 else 1)"

EXPOSE 8080

CMD ["python", "run.py"]
