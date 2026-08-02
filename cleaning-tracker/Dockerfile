FROM python:3.12-slim

# Python block-buffers stdout when it is not a TTY, so every print() in this
# app — the [gcal], [digest], [vps-push] and [notify] lines the whole
# fail-loudly strategy rests on — sat in a buffer until it filled, and was
# effectively absent from `ha addons logs` and journald in real time. Found
# 2026-08-01 while trying to confirm an escalation had fired: the access log
# was there, the app's own lines were not. A diagnostic you cannot read when
# you need it is not a diagnostic.
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Explicit file list, so a new module MUST be added here or the add-on boots
# into ModuleNotFoundError (bridge_watchdog.py, 2026-08-02). Keep in sync with
# every top-level `import <local module>` in app.py.
COPY app.py gcal.py facts.py reconcile.py bridge_watchdog.py notify_ack.py ./

CMD ["python", "app.py"]
