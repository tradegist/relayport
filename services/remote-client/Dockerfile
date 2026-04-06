FROM python:3.11-slim

WORKDIR /app

COPY services/remote-client/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/remote-client/main.py ./
COPY services/remote-client/models_remote_client.py ./
COPY services/remote-client/client/ client/
COPY services/remote-client/routes/ routes/
COPY services/poller/models_poller.py ./
COPY services/notifier/ ./notifier/

CMD ["python", "-u", "main.py"]
