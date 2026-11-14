"""虚构中文工单的装载与演示数据初始化。

数据全部为演示用虚构内容；导入顺序模拟“先导入 6 月，再按月追加新批次”，
便于演示"新批次先匹配已确认主题、低相似度进入待整理区"的流程。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import db, pipeline
from .normalize import CleanRules

DATA = Path(__file__).resolve().parent.parent / "data" / "demo_tickets.json"


def load_demo() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


def seed_database(reset: bool = True, batches: list[str] | None = None) -> dict:
    """按批次顺序导入并训练，留下可回看的运行记录。"""
    if reset:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db.DB_PATH) + suffix)
            if p.exists():
                p.unlink()
    conn = db.connect()
    try:
        data = load_demo()
        plan = [b for b in data["batches"] if not batches or b["id"] in batches]
        config_id = pipeline.ensure_config(conn, CleanRules(), "基线规则")
        log = []
        first = True
        for batch in plan:
            pipeline.import_documents(conn, batch["docs"], batch["id"])
            if first:
                result = pipeline.train(conn, config_id, label=f"首次训练（{batch['id']}）")
                first = False
                # 首批主题视为已确认，后续批次优先按这些主题匹配
                for row in conn.execute("SELECT id FROM topics WHERE status='proposed' ORDER BY id"):
                    conn.execute("UPDATE topics SET status='confirmed', updated_at=? WHERE id=?",
                                 (db.now_iso(), row["id"]))
                conn.commit()
            else:
                result = pipeline.match_batch(conn, config_id, batch["id"], label=f"批次匹配（{batch['id']}）")
            log.append(
                {
                    "batch": batch["id"],
                    "label": batch["label"],
                    "run_id": result.get("run_id"),
                    "summary": result.get("summary", {}),
                }
            )
        # 首批主题默认全部确认为已确认主题，便于演示后续批次优先匹配
        for row in conn.execute("SELECT id FROM topics WHERE status='proposed' ORDER BY id"):
            conn.execute("UPDATE topics SET status='confirmed', updated_at=? WHERE id=?", (db.now_iso(), row["id"]))
        conn.commit()
        db.save_version(conn, "演示数据初始化完成")
        conn.commit()
        return {"config_id": config_id, "batches": log}
    finally:
        conn.close()


def seed_incremental() -> dict:
    """把最后一批（10 月）作为“新到达”批次重新匹配，演示待整理区。"""
    conn = db.connect()
    try:
        data = load_demo()
        batch = data["batches"][-1]
        pipeline.import_documents(conn, batch["docs"], batch["id"])
        config = db.latest_config(conn)
        result = pipeline.match_batch(conn, config["id"], batch["id"], label=f"批次匹配（{batch['id']}）")
        conn.commit()
        return result
    finally:
        conn.close()


if __name__ == "__main__":
    print(json.dumps(seed_database(), ensure_ascii=False, indent=2))
