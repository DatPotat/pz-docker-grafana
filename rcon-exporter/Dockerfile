FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN pip install --no-cache-dir \
        prometheus-client==0.26.0 \
        mcrcon==0.7.0 \
        tzdata==2026.3

COPY rcon_exporter.py scheduler.py ./

CMD ["python", "rcon_exporter.py"]