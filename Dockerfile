FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py bot.py drive_tools.py ./
COPY fonts ./fonts

ENV PORT=8080
EXPOSE 8080

# workers=1: _active_targets/_seen_message_ids dedupe state is in-process memory,
# must stay a single process or the locking logic breaks across instances
CMD exec gunicorn app:app -b 0.0.0.0:${PORT} --workers 1 --threads 4
