@echo off
REM AIRag celery beat: audit retention purge scheduler (daily 03:00). Windows cannot embed beat in the worker.
cd /d %~dp0
.venv\Scripts\python -m celery -A app.workers.celery_app beat --loglevel=info
