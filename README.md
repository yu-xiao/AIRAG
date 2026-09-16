# AIRag 企业知识库

设计文档:docs/AIRag-AI知识库需求设计方案.md
实施计划:docs/superpowers/plans/2026-09-14-airag-implementation-plan.md

## 开发启动(M1 起,原生运行,无需 Docker)

```bash
copy .env.example .env
cd backend
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
net start postgresql-x64-18   # 1/3 确保 PG 服务在跑(需管理员)
start_dev.bat   # 2/3 迁移 + 热重载,后端 http://localhost:8001/docs
cd ..\frontend && pnpm install && pnpm dev   # 3/3 前端 http://localhost:5173
```

## 文档流水线(M2 起)

除后端/前端两个窗口外,再开一个窗口启动 worker:

```bash
cd backend
start_worker.bat
```

上传:.env 填好 ZHIPU_API_KEY 后默认走智谱 embedding;未填 key 时可在 .env 设 EMBED_PROVIDER=fake 跑通全流程(向量无语义)。

Redis:worker 需要 Redis(本机 6379 已有服务)。若该服务设置了 requirepass(本机当前如此),在 .env 配 `REDIS_URL=redis://:<密码>@localhost:6379/0`,否则 worker 连不上。

Worker 带 `-B` 内嵌 beat(M6 起):每日 03:00 自动清理超期审计日志(`AUDIT_RETENTION_DAYS`,默认 180 天,0=禁用)。
