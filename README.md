# AIRag 企业知识库

设计文档:docs/AIRag-AI知识库需求设计方案.md
实施计划:docs/superpowers/plans/2026-09-14-airag-implementation-plan.md

## 开发启动(M1 起,原生运行,无需 Docker)

copy .env.example .env
cd backend
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
start_dev.bat   # 迁移 + 热重载,后端 http://localhost:8000/docs
cd ..\frontend && pnpm install && pnpm dev   # 前端 http://localhost:5173
