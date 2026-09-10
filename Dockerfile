FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Holds the last-trigger timestamp so a restart doesn't re-run an import.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# One worker only: the scheduler and its state live in process memory.
CMD ["gunicorn", "-w", "1", "--threads", "8", "-t", "300", \
     "-b", "0.0.0.0:8080", "app:app"]
