FROM python:3.11-slim

WORKDIR /app

COPY services/market_data/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/market_data/ ./market_data/

CMD ["python", "-u", "-m", "market_data.main"]
