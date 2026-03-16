FROM python:3.11-slim

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY analytics.py .
COPY egregore-setup.zip .
COPY .env.example .env

CMD ["python", "bot.py"]
