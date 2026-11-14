"""导入、清理、去重、首次归类（训练）与增量归类（匹配）。

设计要点
- 清理后的文本进入向量；原文与清理结果一并入库，页面可逐条对照。
- 近似重复的转发/回复并入同一次投递，趋势按"去重后的工单"计数，避免同一条投诉
  被转发多次看起来像上涨。
- 首次训练：本地层次聚类得到候选主题；短文本、空文本、离群记录不强行入簇。
- 后续批次：先按已确认主题质心匹配，相似度不足的记录进入待整理区。
- 重新训练：新旧主题按质心余弦相似度做二部匹配，不依赖簇编号。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import cluster as cluster_mod
from . import db
from .embed import centroid, cosine, explain_terms, fit_idf, vectorize
from .normalize import CleanRules, clean_document, dedupe

CONFIRMED = "confirmed"


@dataclass
class ImportOutcome:
    imported: int
    skipped: int
    duplicates: int
    config_id: int


def _clean_all(conn, config_id: int, rules: CleanRules) -> dict[int, dict]:
    rows = conn.execute("SELECT id, subject, raw_text, submitted_at, batch_id FROM documents ORDER BY id").fetchall()
    cleans: dict[int, dict] = {}
    for row in rows:
        result = clean_document(row["raw_text"], rules, row["subject"] or "")
        payload = result.to_dict()
        conn.execute(
            "INSERT OR REPLACE INTO cleans(config_id, document_id, clean_text, segments_json, removed_chars,"
            " original_chars, dup_key, dup_text) VALUES(?,?,?,?,?,?,?,?)",
            (
                config_id,
                row["id"],
                result.clean_text,
                json.dumps(result.segments, ensure_ascii=False),
                result.removed_chars,
                result.original_chars,
                result.dup_key,
                result.dup_text,
            ),
        )
        cleans[row["id"]] = {
            "id": row["id"],
            "subject": row["subject"] or "",
            "raw_text": row["raw_text"],
            "submitted_at": row["submitted_at"],
            "batch_id": row["batch_id"],
            **payload,
        }
    return cleans


def _dedupe_all(conn, config_id: int, cleans: dict[int, dict], rules: CleanRules) -> dict[int, dict]:
    docs = [{"id": k, "dup_text": v["dup_text"], "support": 1} for k, v in cleans.items()]
    merged = dedupe(docs, threshold=rules.near_duplicate_threshold)
    by_id = {d["id"]: d for d in merged}
    duplicates = 0
    for doc_id, info in by_id.items():
        # 每条重复按"原始投递时间"计数：首次投递的那份决定日期
        conn.execute(
            "INSERT OR REPLACE INTO dedups(config_id, document_id, group_id, is_canonical, duplicate_of,"
            " similarity, support) VALUES(?,?,?,?,?,?,?)",
            (
                config_id,
                doc_id,
                info["duplicate_of"] or doc_id,
                1 if info["canonical"] else 0,
                info["duplicate_of"],
                info["duplicate_similarity"],
                1,
            ),
        )
        if not info["canonical"]:
            duplicates += 1
    return by_id


def _topic_color(conn) -> int:
    used = {row["color_slot"] for row in conn.execute("SELECT color_slot FROM topics WHERE status != 'merged'")}
    for slot in range(1, 9):
        if slot not in used:
            return slot
    return 8


def _name_topic(conn, keywords: list[dict], index: int, existing: list[str], fallback: str = "") -> str:
    """主题名优先用代表工单的标题（来自原文），关键词只做归类线索。

    这样页面上的主题名不会是凭空拼出来的词，也能直接看出"这一簇是谁"；
    用户随时可人工改名，改名后重训仍会沿用。
    """
    terms = [k["term"] for k in keywords[:3]]
    if fallback:
        base = fallback
    elif terms:
        base = "、".join(terms)
    else:
        base = f"未命名主题 {index + 1}"
    candidate = base
    n = 2
    while candidate in existing:
        candidate = f"{base}（{n}）"
        n += 1
    return candidate


def _fallback_name(cleans: dict[int, dict], rep_id: int, index: int) -> str:
    """关键词不足时用代表工单的标题兜底；名称仍需人工确认。"""
    subject = (cleans.get(rep_id) or {}).get("subject", "").strip()
    return subject


def _upsert_topic(conn, *, name, auto_name, status, centroid_vec, keywords, cohesion, run_id, color_slot=None, name_is_custom=False):
    ts = db.now_iso()
    cur = conn.execute(
        "INSERT INTO topics(name, auto_name, name_is_custom, status, color_slot, centroid_json, keywords_json,"
        " cohesion, created_at, updated_at, last_run_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            name,
            auto_name,
            1 if name_is_custom else 0,
            status,
            color_slot or _topic_color(conn),
            json.dumps(centroid_vec, ensure_ascii=False),
            json.dumps(keywords, ensure_ascii=False),
            cohesion,
            ts,
            ts,
            run_id,
        ),
    )
    return int(cur.lastrowid)


def _write_assignment(conn, doc_id, topic_id, method, similarity, reason, run_id, candidate_topic_id=None, candidate_similarity=None):
    conn.execute(
        "INSERT INTO assignments(document_id, topic_id, method, similarity, reason, candidate_topic_id,"
        " candidate_similarity, run_id, updated_at) VALUES(?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(document_id) DO UPDATE SET topic_id=excluded.topic_id, method=excluded.method,"
        " similarity=excluded.similarity, reason=excluded.reason, candidate_topic_id=excluded.candidate_topic_id,"
        " candidate_similarity=excluded.candidate_similarity, run_id=excluded.run_id, updated_at=excluded.updated_at",
        (doc_id, topic_id, method, similarity, reason, candidate_topic_id, candidate_similarity, run_id, db.now_iso()),
    )
    conn.execute(
        "INSERT OR REPLACE INTO memberships(run_id, document_id, topic_id, similarity, method, reason)"
        " VALUES(?,?,?,?,?,?)",
        (run_id, doc_id, topic_id, similarity, method, reason),
    )


def _doc_vector(clean_text: str, idf: dict[str, float]) -> dict[str, float]:
    return vectorize(clean_text, idf)


def _is_short(clean: dict, rules: CleanRules) -> bool:
    text = clean["clean_text"]
    return len(text) < rules.min_chars


def _is_empty(clean: dict, rules: CleanRules) -> bool:
    return not clean["clean_text"].strip()


def _topic_vectors(conn) -> dict[int, dict[str, float]]:
    out = {}
    for row in conn.execute("SELECT id, centroid_json, status, merged_into FROM topics"):
        if row["merged_into"]:
            continue
        try:
            vec = json.loads(row["centroid_json"] or "{}")
        except ValueError:
            vec = {}
        if vec:
            out[row["id"]] = vec
    return out


def train(conn, config_id: int, label: str = "首次训练") -> dict:
    """全量（重新）训练：对所有已清理工单聚类，产出候选主题并做内容对应。"""
    config = db.get_config(conn, config_id)
    rules = CleanRules.from_dict(json.loads(config["rules_json"]))
    cleans = _clean_all(conn, config_id, rules)
    dedup = _dedupe_all(conn, config_id, cleans, rules)
    max_topics = int(db.get_setting(conn, "max_topics", 8))
    run_ts = db.now_iso()

    eligible: list[dict] = []
    unclassifiable: list[dict] = []
    for doc_id, clean in cleans.items():
        info = dedup[doc_id]
        if _is_empty(clean, rules):
            unclassifiable.append({"id": doc_id, "kind": "empty", "reason": "清理后为空文本"})
            continue
        if not info["canonical"]:
            # 重复投递不单独入簇；跟随首次投递
            continue
        if _is_short(clean, rules):
            unclassifiable.append({"id": doc_id, "kind": "short", "reason": f"清理后仅 {len(clean['clean_text'])} 字，样本过短"})
            continue
        eligible.append(clean)

    idf = fit_idf([d["clean_text"] for d in eligible]) if eligible else {}
    vectors = {d["id"]: _doc_vector(d["clean_text"], idf) for d in eligible}
    ordered = [vectors[d["id"]] for d in eligible]
    result = cluster_mod.cluster(
        ordered, max_k=max_topics,
        threshold=float(db.get_setting(conn, "cluster_threshold", 0.12)),
        merge_threshold=float(db.get_setting(conn, "cluster_merge_threshold", 0.10)),
    )

    outlier_ids: set[int] = set()
    cohesion = result["cohesion"]
    # 离群：与所在簇质心相似度显著低于其他成员（均值 - 1.5 标准差，且绝对低于 0.25）
    for j in range(result["k"]):
        members = [i for i, lab in enumerate(result["labels"]) if lab == j]
        if not members:
            continue
        mus = [cosine(vectors[eligible[i]["id"]], result["centers"][j]) for i in members]
        mean = sum(mus) / len(mus)
        std = (sum((s - mean) ** 2 for s in mus) / len(mus)) ** 0.5
        floor = float(db.get_setting(conn, "outlier_threshold", 0.17))
        for i, sim in zip(members, mus):
            if len(members) >= 4 and sim < min(max(floor, 0.25), mean - 1.5 * std):
                outlier_ids.add(eligible[i]["id"])
        if len(members) <= 2 and mean < 0.28:
            # 两三条勉强凑成的簇不可信，整簇留待整理
            outlier_ids.update(eligible[i]["id"] for i in members)

    # 旧主题与本轮候选：按质心相似度做二部匹配，而非看簇编号
    prev_topics = [
        row
        for row in conn.execute(
            "SELECT id, name, auto_name, centroid_json, status, name_is_custom, color_slot FROM topics"
            " WHERE status != 'merged' AND merged_into IS NULL"
        )
        if row["centroid_json"]
    ]
    mapping: list[dict] = []
    candidates = []
    for j in range(result["k"]):
        members = [i for i, lab in enumerate(result["labels"]) if lab == j]
        if not members:
            continue
        center = result["centers"][j]
        keywords = explain_terms([vectors[eligible[i]["id"]] for i in members], center)
        rep = cluster_mod.representative_index([vectors[eligible[i]["id"]] for i in members], center)
        candidates.append(
            {
                "index": j,
                "center": center,
                "keywords": keywords,
                "members": members,
                "representative": eligible[members[rep]]["id"],
                "cohesion": cohesion.get(j, 0.0),
            }
        )

    new_topics: list[dict] = []
    existing_names = [row["name"] for row in prev_topics]
    used_prev: set[int] = set()
    if candidates and prev_topics:
        # 贪心二部匹配：每对只取相似度最高且达到阈值的组合
        pairs = []
        for ci, cand in enumerate(candidates):
            for p_row in prev_topics:
                try:
                    pvec = json.loads(p_row["centroid_json"] or "{}")
                except ValueError:
                    pvec = {}
                if not pvec:
                    continue
                sim = cosine(cand["center"], pvec)
                pairs.append((sim, ci, p_row["id"]))
        pairs.sort(reverse=True)
        matched_c, matched_p = {}, {}
        match_threshold = float(db.get_setting(conn, "mapping_threshold", 0.5))
        for sim, ci, pid in pairs:
            if ci in matched_c or pid in matched_p or sim < match_threshold:
                continue
            matched_c[ci] = (pid, sim)
            matched_p[pid] = (ci, sim)

        # 先给匹配上的候选沿用旧主题（保留人工命名）
        for ci, (pid, sim) in matched_c.items():
            used_prev.add(pid)
            p_row = next(r for r in prev_topics if r["id"] == pid)
            new_topics.append({"candidate": ci, "topic_id": pid, "sim": sim, "reused": True})

    # 未匹配的旧主题：标记为已归档，数据与快照仍可回看，但不再作为当前主题展示。
    # （不删除，因为页面要展示"重新训练后新旧主题的对应关系"以及历史趋势。）
    retired_ids: list[int] = []
    for p_row in prev_topics:
        if p_row["id"] in used_prev:
            continue
        conn.execute("UPDATE topics SET status='retired', updated_at=? WHERE id=?", (run_ts, p_row["id"]))
        retired_ids.append(p_row["id"])

    for ci, cand in enumerate(candidates):
        if any(nt["candidate"] == ci for nt in new_topics):
            continue
        auto = _name_topic(
            conn, cand["keywords"], len(new_topics), existing_names,
            _fallback_name(cleans, cand["representative"], cand["index"]),
        )
        tid = _upsert_topic(
            conn,
            name=auto,
            auto_name=auto,
            status="proposed",
            centroid_vec=cand["center"],
            keywords=cand["keywords"],
            cohesion=cand["cohesion"],
            run_id=None,
        )
        # 记录最接近的旧主题，仅作参考，不自动连成历史趋势
        nearest_prev, nearest_sim = None, 0.0
        for p_row in prev_topics:
            try:
                pvec = json.loads(p_row["centroid_json"] or "{}")
            except ValueError:
                pvec = {}
            if not pvec:
                continue
            sim = cosine(cand["center"], pvec)
            if sim > nearest_sim:
                nearest_prev, nearest_sim = p_row["id"], sim
        new_topics.append(
            {
                "candidate": ci,
                "topic_id": tid,
                "sim": None,
                "reused": False,
                "nearest_prev": nearest_prev,
                "nearest_prev_sim": round(nearest_sim, 4),
            }
        )

    summary = {
        "eligible_docs": len(eligible),
        "duplicate_docs": sum(1 for v in dedup.values() if not v["canonical"]),
        "unclassifiable_docs": len(unclassifiable),
        "k": len(candidates),
        "short_docs": sum(1 for u in unclassifiable if u["kind"] == "short"),
        "empty_docs": sum(1 for u in unclassifiable if u["kind"] == "empty"),
        "outlier_docs": len(outlier_ids),
        "run_label": label,
        "trained_at": run_ts,
        "has_jieba": __import__("app.embed", fromlist=["_HAS_JIEBA"])._HAS_JIEBA,
        "dedupe_threshold": rules.near_duplicate_threshold,
        "min_chars": rules.min_chars,
        "new_topics": len([n for n in new_topics if not n["reused"]]),
        "reused_topics": len([n for n in new_topics if n["reused"]]),
        "retired_topics": len(retired_ids),
    }

    # 写入本次运行的归类
    conn.execute(
        "INSERT INTO runs(kind, config_id, created_at, summary_json, mapping_json, idf_json)"
        " VALUES(?,?,?,?,?,?)",
        ("train", config_id, run_ts, json.dumps(summary, ensure_ascii=False), json.dumps(mapping, ensure_ascii=False), json.dumps(idf)),
    )
    run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    for nt in new_topics:
        cand = candidates[nt["candidate"]]
        conn.execute("UPDATE topics SET last_run_id=? WHERE id=?", (run_id, nt["topic_id"]))
        if not nt["reused"]:
            conn.execute(
                "UPDATE topics SET centroid_json=?, keywords_json=?, cohesion=? WHERE id=?",
                (
                    json.dumps(cand["center"], ensure_ascii=False),
                    json.dumps(cand["keywords"], ensure_ascii=False),
                    cand["cohesion"],
                    nt["topic_id"],
                ),
            )
        else:
            # 沿用旧主题时用融合质心，让后续批次匹配更稳
            pvec = json.loads(conn.execute("SELECT centroid_json FROM topics WHERE id=?", (nt["topic_id"],)).fetchone()[0] or "{}")
            merged = centroid([pvec, cand["center"]])
            conn.execute(
                "UPDATE topics SET centroid_json=?, keywords_json=?, cohesion=? WHERE id=?",
                (
                    json.dumps(merged, ensure_ascii=False),
                    json.dumps(cand["keywords"], ensure_ascii=False),
                    cand["cohesion"],
                    nt["topic_id"],
                ),
            )
        mapping.append(
            {
                "candidate_index": nt["candidate"],
                "candidate_keywords": [k["term"] for k in cand["keywords"]],
                "topic_id": nt["topic_id"],
                "topic_name": conn.execute("SELECT name FROM topics WHERE id=?", (nt["topic_id"],)).fetchone()["name"],
                "similarity": nt["sim"],
                "reused": nt["reused"],
                "members": len(cand["members"]),
                "nearest_prev": nt.get("nearest_prev"),
                "nearest_prev_sim": nt.get("nearest_prev_sim", 0.0),
                "nearest_prev_name": (
                    conn.execute("SELECT name FROM topics WHERE id=?", (nt["nearest_prev"],)).fetchone()["name"]
                    if nt.get("nearest_prev") else None
                ),
            }
        )

    # 旧主题的映射记录（即使未匹配也写一行，便于页面显示"此主题本批无对应"）。
    # 一对一二部匹配下，旧主题可能没有"唯一继承者"，但它的成员可能整体被某个
    # 新主题吸收（例如两个相近主题被合并）。这种情况按成员去向标出，仍然依据内容，
    # 而不是簇编号。
    doc_to_new_topic: dict[int, int] = {}
    for nt in new_topics:
        for i in candidates[nt["candidate"]]["members"]:
            doc_to_new_topic[eligible[i]["id"]] = nt["topic_id"]
    new_topic_ids = {nt["topic_id"] for nt in new_topics}
    for p_row in prev_topics:
        if p_row["id"] in used_prev:
            continue
        best_sim = 0.0
        for cand in candidates:
            try:
                pvec = json.loads(p_row["centroid_json"] or "{}")
            except ValueError:
                pvec = {}
            if pvec:
                best_sim = max(best_sim, cosine(cand["center"], pvec))
        member_ids = [
            row["document_id"]
            for row in conn.execute("SELECT document_id FROM assignments WHERE topic_id=?", (p_row["id"],))
        ]
        moved: dict[int, int] = {}
        for doc_id in member_ids:
            target = doc_to_new_topic.get(doc_id)
            if target in new_topic_ids:
                moved[target] = moved.get(target, 0) + 1
        absorbed_into = max(moved, key=moved.get) if moved else None
        absorbed_count = moved.get(absorbed_into, 0) if absorbed_into else 0
        mapping.append(
            {
                "candidate_index": None,
                "candidate_keywords": [],
                "topic_id": p_row["id"],
                "topic_name": p_row["name"],
                "similarity": round(best_sim, 4),
                "reused": False,
                "members": 0,
                "unmatched": True,
                "member_total": len(member_ids),
                "absorbed_into": (
                    absorbed_into
                    if absorbed_into and absorbed_count >= max(1, (len(member_ids) + 1) // 2)
                    else None
                ),
                "absorbed_count": absorbed_count,
            }
        )

    conn.execute(
        "UPDATE runs SET mapping_json=? WHERE id=?",
        (json.dumps(mapping, ensure_ascii=False), run_id),
    )

    # 归类写入：匹配簇的成员 -> 主题；离群/短/空 -> 待整理（topic_id = NULL）
    for nt in new_topics:
        cand = candidates[nt["candidate"]]
        for i in cand["members"]:
            doc = eligible[i]
            doc_id = doc["id"]
            sim = cosine(vectors[doc_id], cand["center"])
            if doc_id in outlier_ids:
                _write_assignment(
                    conn, doc_id, None, "outlier", round(sim, 4),
                    f"与本批候选主题「{nt['topic_id']}」的相似度偏低、与同簇其他工单差异大，暂不归入",
                    run_id,
                    candidate_topic_id=nt["topic_id"], candidate_similarity=round(sim, 4),
                )
            else:
                _write_assignment(conn, doc_id, nt["topic_id"], "clustered", round(sim, 4),
                                   "首次聚类，与本簇质心最接近", run_id)
    # 重复投递跟随首次投递
    for doc_id, info in dedup.items():
        if info["canonical"]:
            continue
        lead = info["duplicate_of"]
        lead_row = conn.execute("SELECT topic_id, method FROM assignments WHERE document_id=?", (lead,)).fetchone()
        lead_topic = lead_row["topic_id"] if lead_row else None
        _write_assignment(
            conn, doc_id, lead_topic, "duplicate", info["duplicate_similarity"],
            f"与工单 #{lead} 为同一投诉的重复投递（相似度 {info['duplicate_similarity']:.2f}），并入首次投递计数",
            run_id,
        )
    for item in unclassifiable:
        clean = cleans[item["id"]]
        vector = _doc_vector(clean["clean_text"], idf) if clean["clean_text"] else {}
        best_topic, best_sim = None, 0.0
        for nt in new_topics:
            cand = candidates[nt["candidate"]]
            sim = cosine(vector, cand["center"]) if vector else 0.0
            if sim > best_sim:
                best_topic, best_sim = nt["topic_id"], sim
        _write_assignment(
            conn, item["id"], None, item["kind"],
            round(best_sim, 4),
            item["reason"] + ("；最接近的候选主题相似度也仅 %.2f" % best_sim if best_sim else ""),
            run_id, candidate_topic_id=best_topic, candidate_similarity=round(best_sim, 4),
        )

    conn.commit()
    return {
        "run_id": run_id,
        "summary": summary,
        "mapping": mapping,
        "topics": new_topics,
    }


def match_batch(conn, config_id: int, batch_id: str, label: str = "增量匹配") -> dict:
    """新批次先按已确认主题匹配；低相似度进入待整理区。"""
    config = db.get_config(conn, config_id)
    rules = CleanRules.from_dict(json.loads(config["rules_json"]))
    cleans = _clean_all(conn, config_id, rules)
    dedup = _dedupe_all(conn, config_id, cleans, rules)
    topics = list(_topic_vectors(conn).items())
    topic_meta = {
        row["id"]: row
        for row in conn.execute("SELECT id, name, status, merged_into FROM topics")
    }
    threshold = float(db.get_setting(conn, "match_threshold", 0.32))

    # 已确认主题优先；没有已确认的，就用当前所有有效主题（页面会标注为候选）
    confirmed = [t for t in topics if topic_meta.get(t[0]) and topic_meta[t[0]]["status"] == CONFIRMED]
    use_topics = confirmed or topics

    fresh = [d for d in cleans.values() if d["batch_id"] == batch_id]
    run_ts = db.now_iso()
    summary = {
        "run_label": label,
        "trained_at": run_ts,
        "batch_id": batch_id,
        "matched": 0,
        "pending": 0,
        "duplicate_docs": sum(1 for d in fresh if not dedup[d["id"]]["canonical"]),
        "new_docs": len(fresh),
        "threshold": threshold,
        "mode": "confirmed" if confirmed else "proposed",
    }

    conn.execute(
        "INSERT INTO runs(kind, config_id, created_at, summary_json, mapping_json, idf_json)"
        " VALUES(?,?,?,?,?,?)",
        ("match", config_id, run_ts, json.dumps(summary), json.dumps([], ensure_ascii=False), json.dumps({}))
    )
    run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    pending: list[dict] = []
    batch_docs: list[dict] = []
    for clean in fresh:
        info = dedup[clean["id"]]
        if not info["canonical"]:
            lead = info["duplicate_of"]
            lead_row = conn.execute("SELECT topic_id, method FROM assignments WHERE document_id=?", (lead,)).fetchone()
            lead_topic = lead_row["topic_id"] if lead_row else None
            _write_assignment(
                conn, clean["id"], lead_topic, "duplicate", info["duplicate_similarity"],
                f"与工单 #{lead} 内容近似（重复投递，相似度 {info['duplicate_similarity']:.2f}），不计入趋势增量",
                run_id,
            )
            summary["matched"] += 1
            continue
        if _is_empty(clean, rules):
            _write_assignment(conn, clean["id"], None, "empty", 0.0, "清理后为空文本，单列待整理", run_id)
            summary["pending"] += 1
            pending.append({"id": clean["id"], "kind": "empty"})
            continue
        if _is_short(clean, rules):
            _write_assignment(
                conn, clean["id"], None, "short", 0.0,
                f"清理后仅 {len(clean['clean_text'])} 字，样本过短，不强行归入主题", run_id
            )
            summary["pending"] += 1
            pending.append({"id": clean["id"], "kind": "short"})
            continue
        batch_docs.append(clean)

    # 批次内先做一次轻量聚类，帮助页面的"待整理分组"
    idf_row = conn.execute(
        "SELECT idf_json FROM runs WHERE idf_json != '{}' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    idf = json.loads(idf_row["idf_json"]) if idf_row else fit_idf([d["clean_text"] for d in cleans.values()])
    vectors = {d["id"]: _doc_vector(d["clean_text"], idf) for d in batch_docs}

    low_conf: list[int] = []
    for clean in batch_docs:
        doc_id = clean["id"]
        vec = vectors[doc_id]
        sims = [(tid, cosine(vec, tvec)) for tid, tvec in use_topics]
        if not sims:
            pending.append({"id": doc_id, "kind": "unmatched"})
            _write_assignment(conn, doc_id, None, "unmatched", 0.0, "尚无已确认主题，暂存待整理", run_id)
            summary["pending"] += 1
            low_conf.append(doc_id)
            continue
        best = max(sims, key=lambda item: item[1])
        if best[1] >= threshold:
            _write_assignment(
                conn, doc_id, best[0], "matched", round(best[1], 4),
                f"与已确认主题「{topic_meta[best[0]]['name']}」相似度 {best[1]:.2f}，达到匹配阈值 {threshold:.2f}",
                run_id,
            )
            summary["matched"] += 1
        else:
            nearest = max(sims, key=lambda item: item[1])
            _write_assignment(
                conn, doc_id, None, "pending", round(best[1], 4),
                f"与最接近主题「{topic_meta[best[0]]['name']}」相似度仅 {best[1]:.2f}，低于阈值 {threshold:.2f}，进入待整理区",
                run_id,
                candidate_topic_id=best[0],
                candidate_similarity=round(best[1], 4),
            )
            summary["pending"] += 1
            pending.append({"id": doc_id, "kind": "low_similarity", "best_topic": best[0], "best_sim": round(best[1], 4)})
            low_conf.append(doc_id)

    # 待整理区内尝试发现新主题：低相似度记录中的重复内容成簇
    if len(low_conf) >= 3:
        low_vectors = [vectors[d] for d in low_conf]
        res = cluster_mod.cluster(low_vectors, max_k=max(2, min(4, len(low_conf) // 2)))
        created = 0
        existing_names = [row["name"] for row in conn.execute("SELECT name FROM topics")]
        for j in range(res["k"]):
            members = [low_conf[i] for i, lab in enumerate(res["labels"]) if lab == j]
            if len(members) < 3 or res["cohesion"].get(j, 0) < 0.3:
                continue
            center = res["centers"][j]
            member_vecs = [vectors[m] for m in members]
            keywords = explain_terms(member_vecs, center)
            auto = _name_topic(conn, keywords, created, existing_names)
            tid = _upsert_topic(
                conn, name=auto, auto_name=auto, status="proposed", centroid_vec=center,
                keywords=keywords, cohesion=res["cohesion"].get(j, 0.0), run_id=run_id,
            )
            created += 1
            pending.append(
                {
                    "suggested_topic": {"id": tid, "name": auto, "keywords": keywords, "members": len(members)},
                    "documents": members,
                }
            )
            summary.setdefault("suggested_topics", []).append({"id": tid, "name": auto, "members": len(members)})

    conn.commit()
    return {"run_id": run_id, "summary": summary, "pending": pending, "mode": summary["mode"]}


def import_documents(conn, docs: list[dict], batch_id: str) -> ImportOutcome:
    imported = skipped = 0
    for doc in docs:
        text = (doc.get("text") or "").strip()
        if not text:
            skipped += 1
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO documents(ext_id, batch_id, subject, raw_text, submitted_at, source, imported_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                doc.get("id"),
                batch_id,
                doc.get("subject") or "",
                text,
                doc.get("date") or "",
                doc.get("source") or "demo",
                db.now_iso(),
            ),
        )
        if cur.rowcount:
            imported += 1
        else:
            skipped += 1
    conn.commit()
    return ImportOutcome(imported=imported, skipped=skipped, duplicates=0, config_id=0)


def ensure_config(conn, rules: CleanRules, label: str = "基线规则") -> int:
    row = conn.execute("SELECT id FROM configs ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        existing = json.loads(conn.execute("SELECT rules_json FROM configs WHERE id=?", (row["id"],)).fetchone()[0])
        if existing == rules.to_dict():
            return int(row["id"])
    cur = conn.execute(
        "INSERT INTO configs(label, rules_json, created_at) VALUES(?,?,?)",
        (label, json.dumps(rules.to_dict(), ensure_ascii=False), db.now_iso()),
    )
    conn.commit()
    return int(cur.lastrowid)
