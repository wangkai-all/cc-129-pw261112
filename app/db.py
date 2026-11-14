"""SQLite 持久化：工单、清理配置、主题、归类、训练批次、审计与版本快照。"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("TICKET_DB", Path(__file__).resolve().parent.parent / "data" / "tickets.sqlite3"))

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ext_id TEXT UNIQUE,
    batch_id TEXT NOT NULL,
    subject TEXT DEFAULT '',
    raw_text TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    source TEXT DEFAULT 'demo',
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cleans (
    config_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    clean_text TEXT NOT NULL,
    segments_json TEXT NOT NULL,
    removed_chars INTEGER NOT NULL,
    original_chars INTEGER NOT NULL,
    dup_key TEXT NOT NULL,
    dup_text TEXT NOT NULL,
    PRIMARY KEY (config_id, document_id),
    FOREIGN KEY (config_id) REFERENCES configs(id),
    FOREIGN KEY (document_id) REFERENCES documents(id)
);

CREATE TABLE IF NOT EXISTS dedups (
    config_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL,
    is_canonical INTEGER NOT NULL,
    duplicate_of INTEGER,
    similarity REAL NOT NULL,
    support INTEGER NOT NULL,
    PRIMARY KEY (config_id, document_id)
);

CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    auto_name TEXT NOT NULL,
    name_is_custom INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'proposed',
    merged_into INTEGER,
    color_slot INTEGER NOT NULL DEFAULT 1,
    centroid_json TEXT,
    keywords_json TEXT,
    cohesion REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_run_id INTEGER,
    FOREIGN KEY (merged_into) REFERENCES topics(id)
);

CREATE TABLE IF NOT EXISTS assignments (
    document_id INTEGER PRIMARY KEY,
    topic_id INTEGER,
    method TEXT NOT NULL,
    similarity REAL,
    reason TEXT,
    candidate_topic_id INTEGER,
    candidate_similarity REAL,
    run_id INTEGER,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(id),
    FOREIGN KEY (topic_id) REFERENCES topics(id)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    config_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    mapping_json TEXT NOT NULL,
    idf_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS memberships (
    run_id INTEGER NOT NULL,
    document_id INTEGER NOT NULL,
    topic_id INTEGER,
    similarity REAL,
    method TEXT NOT NULL,
    reason TEXT,
    PRIMARY KEY (run_id, document_id)
);

CREATE TABLE IF NOT EXISTS manual_overrides (
    document_id INTEGER PRIMARY KEY,
    topic_id INTEGER,
    action TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    entity_id INTEGER,
    before_json TEXT,
    after_json TEXT,
    note TEXT,
    version_id INTEGER
);

CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    label TEXT NOT NULL,
    state_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "match_threshold": 0.32,
    "outlier_threshold": 0.17,
    "mapping_threshold": 0.5,
    "max_topics": 8,
    # 聚类：主阈值越高主题越细；补合并阈值只合并“两边都至少 2 条”的簇
    "cluster_threshold": 0.11,
    "cluster_merge_threshold": 0.10,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path) if path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, json.dumps(value)))
    conn.commit()
    return conn


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return default


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def latest_config(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM configs ORDER BY id DESC LIMIT 1").fetchone()


def get_config(conn: sqlite3.Connection, config_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM configs WHERE id=?", (config_id,)).fetchone()


def latest_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()


def add_audit(
    conn: sqlite3.Connection,
    action: str,
    entity: str,
    entity_id: int | None,
    before=None,
    after=None,
    note: str = "",
    version_id: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO audit(created_at, action, entity, entity_id, before_json, after_json, note, version_id)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (
            now_iso(),
            action,
            entity,
            entity_id,
            json.dumps(before, ensure_ascii=False) if before is not None else None,
            json.dumps(after, ensure_ascii=False) if after is not None else None,
            note,
            version_id,
        ),
    )
    return int(cur.lastrowid)


def resolve_topic(conn: sqlite3.Connection, topic_id: int | None) -> int | None:
    """沿 merged_into 找到最终主题，防止合并后趋势断线。"""
    seen = set()
    current = topic_id
    while current is not None and current not in seen:
        seen.add(current)
        row = conn.execute("SELECT merged_into FROM topics WHERE id=?", (current,)).fetchone()
        if not row or not row["merged_into"]:
            return current
        current = row["merged_into"]
    return current


def build_state(conn: sqlite3.Connection) -> dict:
    """生成只读快照，用于回看修改前的归类。"""
    topics = []
    for row in conn.execute("SELECT id, name, status, merged_into FROM topics ORDER BY id"):
        topics.append(
            {
                "id": row["id"],
                "name": row["name"],
                "status": row["status"],
                "merged_into": row["merged_into"],
            }
        )
    assignments = []
    for row in conn.execute(
        "SELECT a.document_id, a.topic_id, a.method, a.reason, a.similarity,"
        " d.subject, d.submitted_at, d.batch_id"
        " FROM assignments a JOIN documents d ON d.id=a.document_id ORDER BY a.document_id"
    ):
        assignments.append(
            {
                "document_id": row["document_id"],
                "topic_id": row["topic_id"],
                "method": row["method"],
                "reason": row["reason"],
                "similarity": row["similarity"],
                "subject": row["subject"],
                "submitted_at": row["submitted_at"],
                "batch_id": row["batch_id"],
            }
        )
    return {"topics": topics, "assignments": assignments}


def save_version(conn: sqlite3.Connection, label: str, snapshot: dict | None = None) -> int:
    state = snapshot if snapshot is not None else build_state(conn)
    cur = conn.execute(
        "INSERT INTO versions(created_at, label, state_json) VALUES(?,?,?)",
        (now_iso(), label, json.dumps(state, ensure_ascii=False)),
    )
    return int(cur.lastrowid)


def record_change(
    conn: sqlite3.Connection,
    action: str,
    label: str,
    entity: str,
    entity_id: int | None,
    before=None,
    after=None,
    note: str = "",
    snapshot: dict | None = None,
) -> int:
    """写审计并保存一份状态快照，保证旧归类随时可回看。

    默认保存变更后的状态；调用方若在变更前先 `build_state` 并传入
    `snapshot`，则保存的是变更前的归类，配合 audit 里的 before/after
    可以同时看到“改之前长什么样”和“改了什么”。
    """
    version_id = save_version(conn, label, snapshot)
    add_audit(conn, action, entity, entity_id, before, after, note, version_id)
    conn.commit()
    return version_id


def ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
