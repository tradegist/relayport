FROM python:3.11-slim

WORKDIR /app

COPY poller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY poller/models.py poller/main.py ./
COPY poller/poller/ ./poller/
COPY poller/routes/ ./routes/

CMD ["python", "-u", "main.py"]
