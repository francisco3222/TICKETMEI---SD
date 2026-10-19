FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir prometheus-client
COPY app.py .
EXPOSE 8001
CMD ["python", "app.py"]
