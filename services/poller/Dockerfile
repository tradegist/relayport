FROM python:3.11-slim

WORKDIR /app

COPY services/poller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/poller/models_poller.py services/poller/main.py ./
COPY services/poller/poller/ ./poller/
COPY services/poller/poller_routes/ ./poller_routes/
COPY services/notifier/ ./notifier/
COPY services/dedup/ ./dedup/
COPY services/shared/ ./shared/

CMD ["python", "-u", "main.py"]
