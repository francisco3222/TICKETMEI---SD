# Build a partir da raiz do repositório:  docker build -f order/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY order/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY order/app.py .
EXPOSE 8002
CMD ["python", "app.py"]
