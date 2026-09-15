@echo off
REM AIRag 后端开发启动:迁移 + 热重载
cd /d %~dp0
.venv\Scripts\python -m alembic upgrade head
.venv\Scripts\python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8001
