# Build a partir da raiz do repositório:  docker build -f catalog/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app
# Os dados (data/events.json) e o frontend ficam dentro da imagem (no Kubernetes não há volume ./data)
RUN pip install --no-cache-dir prometheus-client==0.20.0
COPY data/events.json /app/data/events.json
COPY frontend/ /app/static/
COPY catalog/app.py .
# Utilizador sem privilégios (UID numérico): exigido pelo cluster global (PSA "restricted")
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /app/logs && chown -R 10001:10001 /app
USER 10001
EXPOSE 8001
CMD ["python", "app.py"]
