# AIRag 企业知识库

设计文档:docs/AIRag-AI知识库需求设计方案.md
实施计划:docs/superpowers/plans/2026-09-14-airag-implementation-plan.md

## 开发启动(M1 起,原生运行,无需 Docker)

copy .env.example .env
cd backend
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
net start postgresql-x64-18   # 1/3 确保 PG 服务在跑(需管理员)
start_dev.bat   # 2/3 迁移 + 热重载,后端 http://localhost:8001/docs
cd ..\frontend && pnpm install && pnpm dev   # 3/3 前端 http://localhost:5173
cd ..\frontend && pnpm install && pnpm dev   # 前端 http://localhost:5173
