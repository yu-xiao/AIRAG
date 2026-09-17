"""对照运行库清理孤儿评估集(eval_sets/{kb_id}.json 中 KB 已不存在者)。

用法(backend 目录,项目 venv):
    .venv\\Scripts\\python scripts\\purge_orphan_evalsets.py           # dry-run,仅打印
    .venv\\Scripts\\python scripts\\purge_orphan_evalsets.py --apply   # 真删(磁盘 unlink)

M8 背书:验收脚本按 kb_id 留存评估集供复评(git 版本化);系统无 KB 删除端点,
KB 本体经人工清理后文件残留,本脚本按 DB 真值收口。README.md 不在匹配范围。
删除产生的工作区变更随里程碑提交入库。
"""
import argparse
import asyncio
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent.parent / "eval_sets"


def find_orphans(existing_ids: set[int], files: list[Path]) -> list[Path]:
    """文件名主干为纯数字且不在现存 KB id 集合中者即孤儿。"""
    return [
        f for f in files if f.stem.isdigit() and int(f.stem) not in existing_ids
    ]


async def fetch_kb_ids() -> set[int]:
    from sqlalchemy import text

    from app.db.session import SessionLocal

    async with SessionLocal() as s:
        rows = await s.execute(text("SELECT id FROM knowledge_bases"))
        return {r[0] for r in rows.all()}


async def main(apply: bool) -> None:
    ids = await fetch_kb_ids()
    files = sorted(EVAL_DIR.glob("*.json")) if EVAL_DIR.is_dir() else []
    orphans = find_orphans(ids, files)
    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[{mode}] 现存 KB {len(ids)} 个;eval_sets/*.json {len(files)} 个;孤儿 {len(orphans)} 个")
    for f in orphans:
        print(f"  - {f.name}")
    if not orphans:
        print("无可清理文件")
        return
    if apply:
        for f in orphans:
            f.unlink()
            print(f"deleted: {f.name}")
        print("清理完成(工作区变更随里程碑提交)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真删(默认 dry-run)")
    asyncio.run(main(ap.parse_args().apply))
