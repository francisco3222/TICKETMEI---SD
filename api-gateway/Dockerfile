# Build a partir da raiz do repositório:  docker build -f api-gateway/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY api-gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api-gateway/*.py ./
EXPOSE 8000
CMD ["python", "app.py"]
