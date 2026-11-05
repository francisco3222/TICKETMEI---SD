# Build a partir da raiz do repositório:  docker build -f catalog/Dockerfile .
# Os dados (data/events.json) e o frontend ficam dentro da imagem, porque no
# Kubernetes não existe o volume ./data do docker-compose.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app
RUN pip install --no-cache-dir prometheus-client==0.20.0
COPY data/events.json /app/data/events.json
COPY frontend/ /app/static/
COPY catalog/app.py .
EXPOSE 8001
CMD ["python", "app.py"]
