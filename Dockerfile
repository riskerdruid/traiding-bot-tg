FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Moscow

WORKDIR /app

# Зависимости отдельным слоем — пересобираются только при их изменении
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Номер версии приезжает снаружи и становится переменной окружения:
# бот показывает его в ответе на /test, чтобы после автообновления
# было видно, что именно сейчас работает
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

COPY app ./app
COPY tools ./tools
COPY tests ./tests
COPY run.py .

# База и логи живут в томах, чтобы переживать пересборку образа
RUN mkdir -p /app/data /app/logs

# Бот раз в полминуты обновляет файл-отметку. Если она старше трёх минут,
# процесс жив, но цикл событий встал — контейнер надо перезапускать.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import os,sys,time; p='/app/data/heartbeat'; \
        sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 180 else 1)"

EXPOSE 8080

CMD ["python", "run.py"]
