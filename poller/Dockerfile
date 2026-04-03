FROM python:3.11-slim

WORKDIR /app

COPY poller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY models.py .
COPY poller/flex_parser.py .
COPY poller/poller.py .

CMD ["python", "-u", "poller.py"]
