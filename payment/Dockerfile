# Build a partir da raiz do repositório:  docker build -f payment/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY payment/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY payment/app.py .
# Utilizador sem privilégios (UID numérico): exigido pelo cluster global (PSA "restricted")
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /app/logs && chown -R 10001:10001 /app
USER 10001
EXPOSE 8004
CMD ["python", "app.py"]
