FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8080

# One worker only: run state is held in process memory.
# Long threaded timeout so a slow import pass never gets killed.
CMD ["gunicorn", "-w", "1", "--threads", "8", "-t", "300", \
     "-b", "0.0.0.0:8080", "app:app"]
