import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db, pipeline, service  # noqa: E402
from app.normalize import CleanRules  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.sqlite3")
    yield connection
    connection.close()


@pytest.fixture()
def config_id(conn):
    return pipeline.ensure_config(conn, CleanRules(), "测试规则")


DOCS = [
    {
        "id": "A-1",
        "subject": "退款一直没到账",
        "date": "2026-01-03",
        "text": "您好，客服：\n我上周申请的退款，页面显示退款成功，银行卡到现在没有收到钱。麻烦核实一下。\n此致\n敬礼\n王女士\n--\n王某某 | 13800001234",
    },
    {
        "id": "A-2",
        "subject": "退款迟迟不到账",
        "date": "2026-01-05",
        "text": "退款申请已经提交十天了，页面显示完成，但银行卡没有到账。请尽快处理。",
    },
    {
        "id": "A-3",
        "subject": "退款未到账",
        "date": "2026-01-08",
        "text": "退款一直没到账，客服说在催促财务，银行卡仍然没有收到钱。",
    },
    {
        "id": "A-4",
        "subject": "发票抬头开错",
        "date": "2026-01-06",
        "text": "电子发票抬头写错了，税号也不对，财务无法报销，请重新开具。",
    },
    {
        "id": "A-5",
        "subject": "发票信息错误",
        "date": "2026-01-09",
        "text": "发票抬头和税号都错误，需要重开，请把正确抬头核对后再发一份。",
    },
    {
        "id": "A-6",
        "subject": "包装破损",
        "date": "2026-01-10",
        "text": "收到的餐具外包装压坏了，碗碎了两个，申请补发或者退款。",
    },
    {
        "id": "A-7",
        "subject": "包装破损漏液",
        "date": "2026-01-11",
        "text": "商品外包装破损，里面的液体漏了一半，包装盒也压变形了。",
    },
    {
        "id": "A-8",
        "subject": "在吗",
        "date": "2026-01-12",
        "text": "有人吗",
    },
    {
        "id": "A-9",
        "subject": "自动回复",
        "date": "2026-01-13",
        "text": "本邮件由系统自动发送，请勿直接回复本邮件。",
    },
]

RESEND = {
    "id": "A-10",
    "subject": "退款一直没到账",
    "date": "2026-01-14",
    "text": (
        "退款一直没到账\n"
        "-----原始邮件-----\n"
        "发件人：王女士\n"
        "我上周申请的退款，页面显示退款成功，银行卡到现在没有收到钱。\n"
        "此致\n敬礼\n"
        "--\n王某某 | 13800001234"
    ),
}


def test_clean_strips_quote_and_signature_keeps_contrast():
    from app.normalize import clean_document

    result = clean_document(DOCS[0]["text"], CleanRules(), DOCS[0]["subject"])
    kinds = {seg["kind"] for seg in result.segments}
    assert "signature" in kinds
    assert "greeting" in kinds
    assert "李" not in result.clean_text  # 签名被剥离
    assert "此致" not in result.clean_text
    assert "退款" in result.clean_text  # 正文保留
    assert result.original_chars > len(result.clean_text)
    assert any(seg["kind"] == "kept" for seg in result.segments)


def test_dedupes_resend_but_not_short_snippets():
    from app.normalize import clean_document, dedupe

    docs = []
    for raw in DOCS + [RESEND]:
        res = clean_document(raw["text"], CleanRules(), raw["subject"])
        docs.append({"id": raw["id"], "dup_text": res.dup_text, "support": 1})
    merged = dedupe(docs, threshold=0.8)
    by_id = {d["id"]: d for d in merged}
    lead = by_id["A-1"]
    resend = by_id["A-10"]
    assert lead["canonical"] is True
    assert resend["canonical"] is False
    assert resend["duplicate_of"] == "A-1"
    # 短文本 (<10 字) 不会被合并
    assert by_id["A-8"]["canonical"] is True
    # 不同主题的工单不会被合并
    assert by_id["A-4"]["canonical"] is True


def test_trend_counts_duplicates_separately(conn, config_id):
    pipeline.import_documents(conn, DOCS + [RESEND], "2026-01")
    result = pipeline.train(conn, config_id)
    assert result["summary"]["duplicate_docs"] == 1
    docs = service.list_documents(conn)
    trend = service.overall_trend(docs)
    buckets = {b["month"]: b for b in trend["months"]}
    assert buckets["2026-01"]["duplicates"] == 1
    # 10 条原始记录，1 条重复：趋势主体是 9，重复单列
    assert buckets["2026-01"]["tickets"] + buckets["2026-01"]["pending"] == 9
    assert buckets["2026-01"]["tickets"] != 10


def test_short_and_empty_are_separated(conn, config_id):
    pipeline.import_documents(conn, DOCS + [RESEND], "2026-01")
    pipeline.train(conn, config_id)
    pending = service.pending_view(conn)
    kinds = {g["kind"] for g in pending["groups"]}
    assert "short" in kinds
    assert "empty" in kinds
    for group in pending["groups"]:
        for doc in group["documents"]:
            assert doc["topic_id"] is None


def test_unrelated_docs_do_not_get_forced_into_topics(conn, config_id):
    pipeline.import_documents(conn, DOCS + [RESEND], "2026-01")
    pipeline.train(conn, config_id)
    docs = {d["ext_id"]: d for d in service.list_documents(conn)}
    assert docs["A-8"]["method"] == "short"
    assert docs["A-9"]["method"] == "empty"
    assert docs["A-8"]["topic_id"] is None


def test_batch_match_uses_confirmed_topics_and_pends_low_similarity(conn, config_id):
    pipeline.import_documents(conn, DOCS + [RESEND], "2026-01")
    pipeline.train(conn, config_id)
    conn.execute("UPDATE topics SET status='confirmed'")
    conn.commit()

    new_docs = [
        {"id": "B-1", "subject": "退款仍未到账", "date": "2026-02-02",
         "text": "退款申请已经两周，页面显示成功，银行卡还没有到账，请核实。"},
        {"id": "B-2", "subject": "系统登录异常", "date": "2026-02-04",
         "text": "APP 从昨天开始无法登录，一直提示网络异常，换手机也不行。"},
    ]
    pipeline.import_documents(conn, new_docs, "2026-02")
    result = pipeline.match_batch(conn, config_id, "2026-02")
    assert result["summary"]["mode"] == "confirmed"
    docs = {d["ext_id"]: d for d in service.list_documents(conn)}
    assert docs["B-1"]["topic_id"] is not None
    assert docs["B-1"]["topic_name"] == "退款一直没到账" or "退款" in docs["B-1"]["topic_name"]
    assert docs["B-2"]["topic_id"] is None
    assert docs["B-2"]["method"] == "pending"
    assert docs["B-2"]["candidate_similarity"] is not None


def test_retrain_maps_by_content_not_cluster_index(conn, config_id):
    pipeline.import_documents(conn, DOCS + [RESEND], "2026-01")
    pipeline.train(conn, config_id)
    conn.execute("UPDATE topics SET status='confirmed'")
    conn.commit()
    names_before = {t["name"]: t["id"] for t in service.list_topics(conn)}

    extra = [
        {"id": "C-1", "subject": "会员自动续费", "date": "2026-03-03",
         "text": "没有收到续费提醒就扣了会员年费，请关闭自动续费并退款。"},
        {"id": "C-2", "subject": "会员扣费异常", "date": "2026-03-05",
         "text": "会员自动续费扣了一笔钱，但是根本没有提醒，请取消自动续费。"},
    ]
    pipeline.import_documents(conn, extra, "2026-03")
    result = pipeline.train(conn, config_id, label="重训")

    mapping = result["mapping"]
    reused = [m for m in mapping if m["reused"]]
    assert reused, "内容相近的旧主题应被沿用"
    for m in reused:
        assert m["topic_id"] == names_before[m["topic_name"]]
        assert m["similarity"] is not None and m["similarity"] >= 0.5
    new = [m for m in mapping if not m["reused"] and not m.get("unmatched")]
    for m in new:
        assert m["nearest_prev_name"] is not None or m["nearest_prev_sim"] == 0
        assert m["similarity"] is None
    # 至少有一个新主题，其编号与旧主题编号相同也不应直接续接历史
    assert any(m["reused"] is False for m in mapping)


def test_move_document_keeps_before_version(conn, config_id):
    pipeline.import_documents(conn, DOCS + [RESEND], "2026-01")
    pipeline.train(conn, config_id)
    conn.execute("UPDATE topics SET status='confirmed'")
    conn.commit()
    docs = service.list_documents(conn)
    first = next(d for d in docs if d["topic_id"] is not None)
    other = next(t for t in service.list_topics(conn) if t["id"] != first["topic_id"])

    result = service.move_document(conn, first["id"], other["id"])
    assert result["ok"]
    moved = next(d for d in service.list_documents(conn) if d["id"] == first["id"])
    assert moved["topic_id"] == other["id"]
    assert moved["method"] == "manual"

    versions = service.history(conn)["versions"]
    assert versions, "移动后应留下版本快照"
    snapshot = service.get_version(conn, versions[0]["id"])["state"]
    before_assignment = next(a for a in snapshot["assignments"] if a["document_id"] == first["id"])
    # 快照保存的是移动前的状态：仍记录原主题
    assert before_assignment["topic_id"] == first["topic_id"]
    assert before_assignment["method"] != "manual"


def test_merge_snapshot_keeps_both_topics(conn, config_id):
    pipeline.import_documents(conn, DOCS + [RESEND], "2026-01")
    pipeline.train(conn, config_id)
    topics = service.list_topics(conn)
    source, target = topics[0], topics[1]
    source_docs = [d for d in service.list_documents(conn) if d["topic_id"] == source["id"]]
    assert source_docs
    service.merge_topics(conn, source["id"], target["id"])
    version = service.get_version(conn, service.history(conn)["versions"][0]["id"])["state"]
    before_topics = {t["id"]: t for t in version["topics"]}
    assert before_topics[source["id"]]["status"] != "merged"
    assert before_topics[target["id"]]["status"] != "merged"
    before_assignment = next(a for a in version["assignments"] if a["document_id"] == source_docs[0]["id"])
    assert before_assignment["topic_id"] == source["id"]


def test_move_to_pending_uses_parked_method(conn, config_id):
    pipeline.import_documents(conn, DOCS + [RESEND], "2026-01")
    pipeline.train(conn, config_id)
    docs = service.list_documents(conn)
    first = next(d for d in docs if d["topic_id"] is not None)
    service.move_document(conn, first["id"], None)
    docs = {d["id"]: d for d in service.list_documents(conn)}
    assert docs[first["id"]]["topic_id"] is None
    assert docs[first["id"]]["method"] == "parked"
    pending_ids = {d["id"] for g in service.pending_view(conn)["groups"] for d in g["documents"]}
    assert first["id"] in pending_ids


def test_merge_topics_reassigns_and_versions(conn, config_id):
    pipeline.import_documents(conn, DOCS + [RESEND], "2026-01")
    pipeline.train(conn, config_id)
    topics = service.list_topics(conn)
    assert len(topics) >= 2
    source, target = topics[0], topics[1]
    service.merge_topics(conn, source["id"], target["id"])
    merged = {t["id"]: t for t in service.list_topics(conn, include_merged=True)}
    assert merged[source["id"]]["status"] == "merged"
    assert merged[source["id"]]["merged_into"] == target["id"]
    docs = service.list_documents(conn)
    assert all(d["topic_id"] not in (source["id"],) for d in docs)
    assert any(d["topic_id"] == target["id"] and d["method"] == "manual" or d["topic_id"] == target["id"] for d in docs)


def test_rename_persists_custom_name(conn, config_id):
    pipeline.import_documents(conn, DOCS + [RESEND], "2026-01")
    pipeline.train(conn, config_id)
    topic = service.list_topics(conn)[0]
    service.rename_topic(conn, topic["id"], "人工确认的主题名")
    updated = next(t for t in service.list_topics(conn) if t["id"] == topic["id"])
    assert updated["name"] == "人工确认的主题名"
    assert updated["name_is_custom"] is True
    audit = service.history(conn)["audit"]
    assert audit[0]["action"] == "rename_topic"


def test_rules_change_returns_comparison(conn, config_id):
    pipeline.import_documents(conn, DOCS + [RESEND], "2026-01")
    pipeline.train(conn, config_id)
    rules = CleanRules().to_dict()
    rules["strip_greeting"] = False
    result = service.set_rules(conn, rules, retrain=True)
    assert result["comparison"]["a"]["id"] != result["comparison"]["b"]["id"]
    assert result["comparison"]["b"]["topics"]
    assert len(service.history(conn)["versions"]) >= 2


def test_manual_override_reapplied_after_retrain(conn, config_id):
    pipeline.import_documents(conn, DOCS + [RESEND], "2026-01")
    pipeline.train(conn, config_id)
    docs = service.list_documents(conn)
    first = next(d for d in docs if d["topic_id"] is not None)
    target = next(t for t in service.list_topics(conn) if t["id"] != first["topic_id"])
    service.move_document(conn, first["id"], target["id"])
    pipeline.train(conn, config_id, label="重训")
    applied = service.apply_manual_overrides(conn)
    assert applied >= 1
    moved = next(d for d in service.list_documents(conn) if d["id"] == first["id"])
    assert moved["method"] == "manual"
    assert moved["topic_id"] == target["id"]


def test_seed_database_demo_is_incremental(tmp_path, monkeypatch):
    monkeypatch.setenv("TICKET_DB", str(tmp_path / "seed.sqlite3"))
    # db.DB_PATH 在 import 时固定，直接重新导入模块以读取新的环境变量
    import importlib
    import app.db as dbmod
    importlib.reload(dbmod)
    import app.pipeline as pmod
    importlib.reload(pmod)
    import app.seed as seedmod
    importlib.reload(seedmod)

    result = seedmod.seed_database(reset=True)
    assert len(result["batches"]) == 5
    conn = dbmod.connect()
    try:
        stats = service.stats(conn)
    finally:
        conn.close()
    assert stats["documents"] == 84
    assert stats["duplicates"] == 2
    assert stats["topics"] >= 8
