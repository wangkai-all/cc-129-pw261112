"""页面所需的查询与操作：主题视图、趋势、原因解释、人工改名/合并/移动、历史回看。"""

from __future__ import annotations

import json
from collections import defaultdict

from . import db
from . import pipeline

PENDING_METHODS = {"pending", "short", "empty", "outlier", "unmatched", "parked"}
METHOD_LABELS = {
    "clustered": "首次聚类",
    "matched": "匹配已确认主题",
    "manual": "人工移动",
    "parked": "人工移入待整理",
    "duplicate": "重复投递",
    "pending": "低相似度待整理",
    "short": "短文本待整理",
    "empty": "空文本待整理",
    "outlier": "离群待整理",
    "unmatched": "尚未匹配",
}


def month_of(iso: str) -> str:
    return (iso or "")[:7] or "未知"


def _display_name(row) -> str:
    return row["name"]


# ---------------------------------------------------------------------------
# 文档
# ---------------------------------------------------------------------------

def list_documents(conn, *, topic_id: str | None = None, status: str | None = None, batch: str | None = None,
                   q: str | None = None, limit: int = 500) -> list[dict]:
    latest_config = db.latest_config(conn)
    config_id = latest_config["id"] if latest_config else 0
    rows = conn.execute(
        """SELECT d.id, d.ext_id, d.batch_id, d.subject, d.raw_text, d.submitted_at,
                  a.topic_id, a.method, a.similarity, a.reason,
                  a.candidate_topic_id, a.candidate_similarity,
                  c.clean_text, c.segments_json, c.removed_chars, c.original_chars,
                  dd.is_canonical, dd.duplicate_of, dd.similarity AS duplicate_similarity,
                  t.name AS topic_name, t.name_is_custom, t.status AS topic_status,
                  ct.name AS candidate_name
           FROM documents d
           LEFT JOIN assignments a ON a.document_id = d.id
           LEFT JOIN cleans c ON c.document_id = d.id AND c.config_id = ?
           LEFT JOIN dedups dd ON dd.document_id = d.id AND dd.config_id = ?
           LEFT JOIN topics t ON t.id = a.topic_id
           LEFT JOIN topics ct ON ct.id = a.candidate_topic_id
           ORDER BY d.submitted_at, d.id""",
        (config_id, config_id),
    ).fetchall()
    out = []
    for row in rows:
        method = row["method"] or "unmatched"
        doc = {
            "id": row["id"],
            "ext_id": row["ext_id"],
            "batch_id": row["batch_id"],
            "subject": row["subject"],
            "submitted_at": row["submitted_at"],
            "month": month_of(row["submitted_at"]),
            "clean_text": row["clean_text"] or "",
            "original_chars": row["original_chars"] or 0,
            "removed_chars": row["removed_chars"] or 0,
            "segments": json.loads(row["segments_json"] or "[]"),
            "topic_id": row["topic_id"],
            "topic_name": row["topic_name"],
            "topic_status": row["topic_status"],
            "method": method,
            "method_label": METHOD_LABELS.get(method, method),
            "similarity": row["similarity"],
            "reason": row["reason"],
            "candidate_topic_id": row["candidate_topic_id"],
            "candidate_topic_name": row["candidate_name"],
            "candidate_similarity": row["candidate_similarity"],
            "is_canonical": bool(row["is_canonical"] if row["is_canonical"] is not None else 1),
            "duplicate_of": row["duplicate_of"],
            "duplicate_similarity": row["duplicate_similarity"],
        }
        if topic_id is not None:
            if topic_id == "none":
                if doc["topic_id"] is not None:
                    continue
            elif str(doc["topic_id"]) != str(topic_id):
                continue
        if status == "pending" and method not in PENDING_METHODS:
            continue
        if status == "duplicate" and method != "duplicate":
            continue
        if status and status not in ("pending", "duplicate") and method != status:
            continue
        if batch and doc["batch_id"] != batch:
            continue
        if q:
            needle = q.lower()
            hay = (doc["subject"] + " " + (row["raw_text"] or "") + " " + doc["clean_text"]).lower()
            if needle not in hay:
                continue
        out.append(doc)
        if len(out) >= limit:
            break
    return out


def get_document(conn, doc_id: int) -> dict | None:
    docs = {d["id"]: d for d in list_documents(conn)}
    return docs.get(doc_id)


# ---------------------------------------------------------------------------
# 主题与趋势
# ---------------------------------------------------------------------------

def _trend_from_docs(docs: list[dict]) -> list[dict]:
    """按月统计；重复投递不计数，另附 support 供页面核对“转发不算趋势”。"""
    buckets: dict[str, dict] = {}
    for doc in docs:
        month = doc["month"]
        slot = buckets.setdefault(month, {"month": month, "tickets": 0, "mentions": 0})
        if not doc["is_canonical"] or doc["method"] == "duplicate":
            slot["mentions"] += 1
            continue
        slot["tickets"] += 1
        slot["mentions"] += 1
    return [buckets[m] for m in sorted(buckets)]


def overall_trend(docs: list[dict]) -> dict:
    buckets: dict[str, dict] = {}
    for doc in docs:
        month = doc["month"]
        slot = buckets.setdefault(month, {"month": month, "tickets": 0, "duplicates": 0, "pending": 0})
        if doc["method"] == "duplicate":
            slot["duplicates"] += 1
            continue
        if doc["topic_id"] is None:
            slot["pending"] += 1
            continue
        slot["tickets"] += 1
    return {"months": [buckets[m] for m in sorted(buckets)], "total_docs": len(docs)}


def _topic_docs(docs: list[dict], topic_ids: set[int]) -> list[dict]:
    return [d for d in docs if d["topic_id"] in topic_ids]


def _resolve_children(conn, topic_id: int) -> list[int]:
    ids = [topic_id]
    for row in conn.execute("SELECT id FROM topics WHERE merged_into=?", (topic_id,)):
        ids.extend(_resolve_children(conn, row["id"]))
    return ids


def _why(row, docs: list[dict], siblings: list[dict]) -> list[str]:
    """用可核对的事实解释“为什么放在一起”，不用关键词编造原文没有的事实。"""
    reasons: list[str] = []
    keywords = json.loads(row["keywords_json"] or "[]")
    if keywords:
        terms = "、".join(k["term"] for k in keywords[:4])
        reasons.append(f"这些工单的向量中心附近反复出现这些归类线索：{terms}（仅作线索，不代表原文结论）。")
    if row["cohesion"] is not None:
        reasons.append(f"成员与主题中心的平均余弦相似度为 {row['cohesion']:.2f}；相似度越高，用词越接近。")
    sims = [d["similarity"] for d in docs if d["similarity"] is not None and d["method"] in ("clustered", "matched")]
    if sims:
        low, high = min(sims), max(sims)
        reasons.append(f"成员与中心的相似度落在 {low:.2f}–{high:.2f} 之间；低于阈值的记录没有被算进来。")
    dup = [d for d in docs if d["method"] == "duplicate"]
    if dup:
        reasons.append(f"另有 {len(dup)} 条重复投递已并入首次投递，不重复计入趋势。")
    return reasons


COLOR_SLOTS = 8


def _assign_colors(topics: list[dict]) -> None:
    """按主题的稳定顺序分配 1..8 色槽；筛选不会改变某个主题的颜色。"""
    visible = sorted(
        (t for t in topics if t["status"] in ("confirmed", "proposed")),
        key=lambda t: t["id"],
    )
    for idx, topic in enumerate(visible):
        topic["color_slot"] = idx % COLOR_SLOTS + 1


def list_topics(conn, *, include_merged: bool = False) -> list[dict]:
    docs = list_documents(conn)
    topics = conn.execute(
        "SELECT * FROM topics WHERE merged_into IS NULL OR status='merged' ORDER BY id"
    ).fetchall()
    out = []
    for row in topics:
        if row["status"] in ("merged", "retired") and not include_merged:
            continue
        children = _resolve_children(conn, row["id"])
        member_docs = [d for d in docs if d["topic_id"] in children]
        canonical = [d for d in member_docs if d["is_canonical"] and d["method"] != "duplicate"]
        representatives = sorted(
            [d for d in canonical if d["method"] in ("clustered", "matched", "manual")],
            key=lambda d: -(d["similarity"] or 0),
        )[:3]
        merged_children = [
            {"id": r["id"], "name": r["name"]}
            for r in conn.execute("SELECT id, name FROM topics WHERE merged_into=?", (row["id"],))
        ]
        out.append(
            {
                "id": row["id"],
                "name": row["name"],
                "auto_name": row["auto_name"],
                "name_is_custom": bool(row["name_is_custom"]),
                "status": row["status"],
                "merged_into": row["merged_into"],
                "color_slot": row["color_slot"],
                "keywords": json.loads(row["keywords_json"] or "[]"),
                "cohesion": row["cohesion"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "doc_count": len(member_docs),
                "canonical_count": len(canonical),
                "trend": _trend_from_docs(member_docs),
                "representatives": [
                    {
                        "id": d["id"],
                        "subject": d["subject"],
                        "date": d["submitted_at"],
                        "excerpt": (d["clean_text"] or "")[:80],
                        "similarity": d["similarity"],
                        "method": d["method"],
                        "method_label": d["method_label"],
                    }
                    for d in representatives
                ],
                "why": _why(row, member_docs, canonical),
                "merged_children": merged_children,
            }
        )
    _assign_colors(out)
    return out


def get_topic(conn, topic_id: int) -> dict | None:
    topics = {t["id"]: t for t in list_topics(conn, include_merged=True)}
    topic = topics.get(topic_id)
    if not topic:
        return None
    children = _resolve_children(conn, topic_id)
    topic["documents"] = [d for d in list_documents(conn) if d["topic_id"] in children]
    topic["candidate_similarity_threshold"] = db.get_setting(conn, "match_threshold", 0.32)
    return topic


def pending_view(conn) -> dict:
    docs = list_documents(conn, status="pending")
    by_kind = defaultdict(list)
    for doc in docs:
        by_kind[doc["method"]].append(doc)
    return {
        "total": len(docs),
        "groups": [
            {"kind": kind, "label": METHOD_LABELS.get(kind, kind), "documents": items}
            for kind, items in sorted(by_kind.items())
        ],
    }


# ---------------------------------------------------------------------------
# 操作
# ---------------------------------------------------------------------------

def _doc_row(conn, doc_id: int):
    return conn.execute(
        "SELECT a.*, d.subject, d.submitted_at FROM assignments a JOIN documents d ON d.id=a.document_id"
        " WHERE a.document_id=?", (doc_id,)
    ).fetchone()


def rename_topic(conn, topic_id: int, name: str) -> dict:
    before = conn.execute("SELECT name, name_is_custom FROM topics WHERE id=?", (topic_id,)).fetchone()
    if not before:
        raise KeyError("主题不存在")
    name = (name or "").strip()
    if not name:
        raise ValueError("主题名称不能为空")
    snapshot = db.build_state(conn)
    conn.execute("UPDATE topics SET name=?, name_is_custom=1, updated_at=? WHERE id=?", (name, db.now_iso(), topic_id))
    db.record_change(
        conn, "rename_topic", f"改名前：{before['name']}", "topic", topic_id,
        {"name": before["name"]}, {"name": name}, "人工修改主题名称，快照保留改名前的显示名",
        snapshot=snapshot,
    )
    return {"ok": True, "name": name}


def confirm_topic(conn, topic_id: int) -> dict:
    row = conn.execute("SELECT name, status FROM topics WHERE id=?", (topic_id,)).fetchone()
    if not row:
        raise KeyError("主题不存在")
    conn.execute("UPDATE topics SET status='confirmed', updated_at=? WHERE id=?", (db.now_iso(), topic_id))
    db.record_change(conn, "confirm_topic", f"确认主题：{row['name']}", "topic", topic_id,
                     {"status": row["status"]}, {"status": "confirmed"}, "新批次将优先匹配此主题")
    return {"ok": True}


def merge_topics(conn, source_id: int, target_id: int) -> dict:
    if source_id == target_id:
        raise ValueError("不能把主题合并到自己")
    src = conn.execute("SELECT name, status FROM topics WHERE id=? AND merged_into IS NULL", (source_id,)).fetchone()
    dst = conn.execute("SELECT name FROM topics WHERE id=? AND merged_into IS NULL", (target_id,)).fetchone()
    if not src or not dst:
        raise KeyError("主题不存在或已被合并")
    snapshot = db.build_state(conn)
    conn.execute("UPDATE topics SET status='merged', merged_into=?, updated_at=? WHERE id=?",
                 (target_id, db.now_iso(), source_id))
    conn.execute("UPDATE assignments SET topic_id=?, updated_at=? WHERE topic_id=?",
                 (target_id, db.now_iso(), source_id))
    db.record_change(
        conn, "merge_topics", f"合并前：{src['name']} → {dst['name']}", "topic", source_id,
        {"source": src["name"], "target": dst["name"]},
        {"merged_into": target_id},
        "合并后历史趋势按目标主题回看，原主题保留映射；快照保留合并前的两个主题",
        snapshot=snapshot,
    )
    return {"ok": True}


def move_document(conn, doc_id: int, target_topic_id: int | None, note: str = "") -> dict:
    doc = _doc_row(conn, doc_id)
    if not doc:
        raise KeyError("工单不存在")
    target = None
    if target_topic_id is not None:
        target = db.resolve_topic(conn, target_topic_id)
        if not target or not conn.execute("SELECT 1 FROM topics WHERE id=? AND merged_into IS NULL", (target,)).fetchone():
            raise KeyError("目标主题不存在")
    before = {"topic_id": doc["topic_id"], "method": doc["method"]}
    method = "manual" if target is not None else "parked"
    snapshot = db.build_state(conn)
    conn.execute(
        "INSERT INTO assignments(document_id, topic_id, method, similarity, reason, candidate_topic_id,"
        " candidate_similarity, run_id, updated_at) VALUES(?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(document_id) DO UPDATE SET topic_id=excluded.topic_id, method=excluded.method,"
        " reason=excluded.reason, updated_at=excluded.updated_at",
        (
            doc_id,
            target,
            method,
            doc["similarity"],
            note or ("人工移动到此主题" if target else "人工移入待整理区"),
            doc["candidate_topic_id"],
            doc["candidate_similarity"],
            doc["run_id"],
            db.now_iso(),
        ),
    )
    conn.execute(
        "INSERT INTO manual_overrides(document_id, topic_id, action, updated_at) VALUES(?,?,?,?)"
        " ON CONFLICT(document_id) DO UPDATE SET topic_id=excluded.topic_id, action=excluded.action,"
        " updated_at=excluded.updated_at",
        (doc_id, target, "move", db.now_iso()),
    )
    db.record_change(
        conn, "move_document", f"移动前：{doc['subject']}", "document", doc_id,
        before, {"topic_id": target}, note or "人工移动，快照保留修改前的归类",
        snapshot=snapshot,
    )
    return {"ok": True}


def apply_manual_overrides(conn) -> int:
    """重新训练/规则调整后，把有效的人工移动重新应用到新结果上。

    目标主题被合并时跟随合并去向；目标主题已被归档或不存在时放弃这条人工移动，
    否则工单会挂在一个不再展示的旧主题上，从页面上“消失”。
    """
    rows = conn.execute(
        "SELECT m.document_id, m.topic_id, t.merged_into, t.status FROM manual_overrides m"
        " LEFT JOIN topics t ON t.id=m.topic_id"
    ).fetchall()
    applied = 0
    for row in rows:
        target = None
        if row["topic_id"] is not None:
            target = db.resolve_topic(conn, row["topic_id"])
            if target is None:
                continue
            current = conn.execute("SELECT status, merged_into FROM topics WHERE id=?", (target,)).fetchone()
            if not current or current["merged_into"] is not None or current["status"] in ("retired", "merged"):
                continue
        conn.execute(
            "UPDATE assignments SET topic_id=?, method=?, updated_at=? WHERE document_id=?",
            (target, "manual" if target is not None else "parked", db.now_iso(), row["document_id"]),
        )
        applied += 1
    conn.commit()
    return applied


def set_rules(conn, rules: dict, retrain: bool = True) -> dict:
    """调整清理规则：保存为新配置，按当前语料重新清理；默认一并重训。"""
    from .normalize import CleanRules

    parsed = CleanRules.from_dict(rules)
    before = db.latest_config(conn)
    label = f"规则 v{(before['id'] + 1) if before else 1}"
    config_id = pipeline.ensure_config(conn, parsed, label)
    db.record_change(
        conn, "update_rules", f"调整清理规则 → {label}", "config", config_id,
        json.loads(before["rules_json"]) if before else None,
        parsed.to_dict(), "规则变更保存为新配置，旧配置与旧归类仍可回看",
    )
    result = {"config_id": config_id, "label": label}
    if retrain:
        before_version = db.save_version(conn, "规则变更前")
        result["train"] = pipeline.train(conn, config_id, label=f"{label} 重新训练")
        applied = apply_manual_overrides(conn)
        after_version = db.save_version(conn, f"{label} 重新训练后")
        conn.commit()
        result["manual_reapplied"] = applied
        result["before_version"] = before_version
        result["after_version"] = after_version
        result["comparison"] = compare_versions(conn, before_version, after_version)
    return result


def compare_versions(conn, version_a: int, version_b: int) -> dict:
    """比较两个快照的归类与趋势，用于“修改前后”的对照。"""
    rows = conn.execute("SELECT * FROM versions WHERE id IN (?,?)", (version_a, version_b)).fetchall()
    by_id = {r["id"]: json.loads(r["state_json"]) for r in rows}
    if version_a not in by_id or version_b not in by_id:
        raise KeyError("版本不存在")

    def summarize(state: dict) -> dict:
        topics = {t["id"]: t for t in state.get("topics", [])}
        buckets: dict[int | None, dict[str, int]] = {}
        for a in state.get("assignments", []):
            if a.get("method") == "duplicate":
                continue
            bucket = buckets.setdefault(a.get("topic_id"), {"count": 0})
            bucket["count"] += 1
            month = month_of(a.get("submitted_at", ""))
            bucket[month] = bucket.get(month, 0) + 1
        return {
            "topics": [
                {
                    "id": tid,
                    "name": topics.get(tid, {}).get("name", "未归类"),
                    "status": topics.get(tid, {}).get("status", "pending"),
                    "count": info["count"],
                    "months": {k: v for k, v in info.items() if k != "count"},
                }
                for tid, info in buckets.items()
            ]
        }

    return {"a": {"id": version_a, **summarize(by_id[version_a])}, "b": {"id": version_b, **summarize(by_id[version_b])}}


def history(conn) -> dict:
    versions = [
        {
            "id": r["id"],
            "created_at": r["created_at"],
            "label": r["label"],
        }
        for r in conn.execute("SELECT id, created_at, label FROM versions ORDER BY id DESC LIMIT 100")
    ]
    audit = [
        {
            "id": r["id"],
            "created_at": r["created_at"],
            "action": r["action"],
            "entity": r["entity"],
            "entity_id": r["entity_id"],
            "note": r["note"],
            "before": json.loads(r["before_json"]) if r["before_json"] else None,
            "after": json.loads(r["after_json"]) if r["after_json"] else None,
            "version_id": r["version_id"],
        }
        for r in conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200")
    ]
    runs = [
        {
            "id": r["id"],
            "kind": r["kind"],
            "created_at": r["created_at"],
            "summary": json.loads(r["summary_json"]),
            "mapping": json.loads(r["mapping_json"]),
        }
        for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 30")
    ]
    return {"versions": versions, "audit": audit, "runs": runs}


def get_version(conn, version_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
    if not row:
        return None
    return {"id": row["id"], "label": row["label"], "created_at": row["created_at"],
            "state": json.loads(row["state_json"])}


def stats(conn) -> dict:
    docs = list_documents(conn)
    topics = list_topics(conn)
    return {
        "documents": len(docs),
        "duplicates": sum(1 for d in docs if d["method"] == "duplicate"),
        "pending": sum(1 for d in docs if d["topic_id"] is None and d["method"] != "duplicate"),
        "short": sum(1 for d in docs if d["method"] == "short"),
        "empty": sum(1 for d in docs if d["method"] == "empty"),
        "outliers": sum(1 for d in docs if d["method"] == "outlier"),
        "topics": len(topics),
        "confirmed": sum(1 for t in topics if t["status"] == "confirmed"),
        "batches": [
            {"id": r["batch_id"], "count": r["n"], "first": r["first"], "last": r["last"]}
            for r in conn.execute(
                "SELECT batch_id, COUNT(*) AS n, MIN(submitted_at) AS first, MAX(submitted_at) AS last"
                " FROM documents GROUP BY batch_id ORDER BY batch_id"
            )
        ],
        "overall_trend": overall_trend(docs),
    }
