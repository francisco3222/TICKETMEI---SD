# Build a partir da raiz do repositório:  docker build -f notification/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY notification/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY notification/app.py .
EXPOSE 8003
CMD ["python", "app.py"]
