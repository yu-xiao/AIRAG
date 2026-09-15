@echo off
REM AIRag 文档流水线 worker（Windows 必须 solo 池）
cd /d %~dp0
.venv\Scripts\python -m celery -A app.workers.celery_app worker --pool=solo --loglevel=info
