FROM python:3.12-slim

LABEL maintainer="Ryan Swindle <rswindle@gmail.com>"
LABEL description="ASCOM Alpaca server for the Birger EF-232 Canon EF lens controller"

WORKDIR /alpyca

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY config.yaml .
COPY src ./src

CMD ["python", "src/main.py"]
