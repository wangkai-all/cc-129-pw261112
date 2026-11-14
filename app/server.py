"""HTTP API（标准库 http.server，无第三方依赖）与静态页面。"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import db, pipeline, service
from .normalize import CleanRules

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


def _json_default(obj):
    return str(obj)


class Handler(BaseHTTPRequestHandler):
    server_version = "TicketTopics/1.0"

    # ---- helpers -------------------------------------------------------
    def _send(self, status: int, payload, content_type: str = "application/json; charset=utf-8"):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str):
        self._send(status, {"error": message})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def log_message(self, fmt, *args):  # 安静一些
        if os.environ.get("HTTP_VERBOSE"):
            super().log_message(fmt, *args)

    def _conn(self):
        return db.connect()

    # ---- routing -------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                self._api_get(path, query)
            else:
                self._static(path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # pragma: no cover - 兜底
            self._error(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            body = self._read_json()
            self._api_post(path, body)
        except KeyError as exc:
            self._error(404, str(exc))
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:  # pragma: no cover - 兜底
            self._error(500, f"{type(exc).__name__}: {exc}")

    def _static(self, path: str):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (WEB / rel).resolve()
        if not str(target).startswith(str(WEB.resolve())) or not target.is_file():
            self._error(404, "not found")
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    def _api_get(self, path: str, query: dict):
        conn = self._conn()
        try:
            if path == "/api/health":
                self._send(200, {"ok": True})
            elif path == "/api/overview":
                self._send(200, service.stats(conn))
            elif path == "/api/topics":
                self._send(200, {"topics": service.list_topics(conn, include_merged=query.get("merged", ["0"])[0] == "1")})
            elif path == "/api/topic":
                topic_id = int(query.get("id", ["0"])[0])
                topic = service.get_topic(conn, topic_id)
                if not topic:
                    self._error(404, "主题不存在")
                else:
                    self._send(200, topic)
            elif path == "/api/documents":
                docs = service.list_documents(
                    conn,
                    topic_id=query.get("topic_id", [None])[0],
                    status=query.get("status", [None])[0],
                    batch=query.get("batch", [None])[0],
                    q=query.get("q", [None])[0],
                )
                self._send(200, {"documents": docs})
            elif path == "/api/batches":
                from .seed import load_demo

                imported = {
                    r["batch_id"]: r["n"]
                    for r in conn.execute("SELECT batch_id, COUNT(*) AS n FROM documents GROUP BY batch_id")
                }
                data = load_demo()
                self._send(200, {
                    "batches": [
                        {
                            "id": b["id"],
                            "label": b["label"],
                            "count": len(b["docs"]),
                            "imported": imported.get(b["id"], 0),
                            "documents": b["docs"] if not imported.get(b["id"]) else [],
                        }
                        for b in data["batches"]
                    ]
                })
            elif path == "/api/pending":
                self._send(200, service.pending_view(conn))
            elif path == "/api/history":
                self._send(200, service.history(conn))
            elif path == "/api/compare":
                a = int(query.get("a", ["0"])[0])
                b = int(query.get("b", ["0"])[0])
                self._send(200, service.compare_versions(conn, a, b))
            elif path == "/api/version":
                version = service.get_version(conn, int(query.get("id", ["0"])[0]))
                if not version:
                    self._error(404, "版本不存在")
                else:
                    self._send(200, version)
            elif path == "/api/config":
                config = db.latest_config(conn)
                self._send(200, {
                    "config_id": config["id"] if config else None,
                    "label": config["label"] if config else None,
                    "rules": json.loads(config["rules_json"]) if config else CleanRules().to_dict(),
                    "settings": {
                        "match_threshold": db.get_setting(conn, "match_threshold"),
                        "outlier_threshold": db.get_setting(conn, "outlier_threshold"),
                        "mapping_threshold": db.get_setting(conn, "mapping_threshold"),
                        "max_topics": db.get_setting(conn, "max_topics"),
                        "cluster_threshold": db.get_setting(conn, "cluster_threshold"),
                        "cluster_merge_threshold": db.get_setting(conn, "cluster_merge_threshold"),
                    },
                })
            else:
                self._error(404, "unknown endpoint")
        finally:
            conn.close()

    def _api_post(self, path: str, body: dict):
        conn = self._conn()
        try:
            if path == "/api/import":
                batch = body.get("batch") or "manual"
                docs = body.get("documents") or []
                outcome = pipeline.import_documents(conn, docs, batch)
                config = db.latest_config(conn)
                if not config:
                    raise ValueError("请先在设置里保存清理规则")
                mode = body.get("mode") or "match"
                if mode == "train":
                    result = pipeline.train(conn, config["id"], label=f"导入后训练：{batch}")
                else:
                    result = pipeline.match_batch(conn, config["id"], batch)
                service.apply_manual_overrides(conn)
                self._send(200, {
                    "imported": outcome.imported,
                    "skipped": outcome.skipped,
                    "run": {k: v for k, v in result.items() if k != "pending"},
                })
            elif path == "/api/rules":
                result = service.set_rules(conn, body.get("rules") or {}, retrain=bool(body.get("retrain", True)))
                self._send(200, result)
            elif path == "/api/retrain":
                config = db.latest_config(conn)
                if not config:
                    raise ValueError("尚未保存清理规则")
                before_version = db.save_version(conn, "重新训练前")
                result = pipeline.train(conn, config["id"], label=body.get("label") or "重新训练")
                applied = service.apply_manual_overrides(conn)
                after_version = db.save_version(conn, "重新训练后")
                conn.commit()
                self._send(200, {**result, "manual_reapplied": applied,
                                 "before_version": before_version, "after_version": after_version,
                                 "comparison": service.compare_versions(conn, before_version, after_version)})
            elif path == "/api/import-batch":
                from .seed import load_demo

                batch_id = body.get("batch")
                if not batch_id:
                    raise ValueError("缺少批次")
                data = load_demo()
                batch = next((b for b in data["batches"] if b["id"] == batch_id), None)
                if not batch:
                    raise KeyError("批次不存在")
                outcome = pipeline.import_documents(conn, batch["docs"], batch["id"])
                config = db.latest_config(conn)
                if not config:
                    raise ValueError("请先保存清理规则")
                mode = body.get("mode") or "match"
                if mode == "train":
                    result = pipeline.train(conn, config["id"], label=f"导入后训练：{batch_id}")
                else:
                    result = pipeline.match_batch(conn, config["id"], batch_id)
                service.apply_manual_overrides(conn)
                self._send(200, {"imported": outcome.imported, "skipped": outcome.skipped, "run": result})
            elif path == "/api/match":
                config = db.latest_config(conn)
                batch = body.get("batch")
                if not batch:
                    raise ValueError("缺少批次")
                result = pipeline.match_batch(conn, config["id"], batch, label=body.get("label") or "批次匹配")
                service.apply_manual_overrides(conn)
                self._send(200, result)
            elif path == "/api/topic/rename":
                self._send(200, service.rename_topic(conn, int(body["id"]), body.get("name", "")))
            elif path == "/api/topic/confirm":
                self._send(200, service.confirm_topic(conn, int(body["id"])))
            elif path == "/api/topic/merge":
                self._send(200, service.merge_topics(conn, int(body["source"]), int(body["target"])))
            elif path == "/api/document/move":
                target = body.get("topic_id")
                self._send(200, service.move_document(conn, int(body["id"]),
                                                       int(target) if target not in (None, "", "null") else None,
                                                       body.get("note", "")))
            elif path == "/api/settings":
                for key in (
                    "match_threshold", "outlier_threshold", "mapping_threshold", "max_topics",
                    "cluster_threshold", "cluster_merge_threshold",
                ):
                    if key in body:
                        db.set_setting(conn, key, body[key])
                conn.commit()
                db.record_change(conn, "update_settings", "调整匹配阈值", "settings", None, None, body,
                                 "阈值调整会影响后续批次匹配与重训对应")
                self._send(200, {"ok": True})
            elif path == "/api/reset":
                conn.close()
                _reset_db()
                self._send(200, {"ok": True})
            elif path == "/api/demo/load":
                from .seed import seed_database

                conn.close()
                seed_database(reset=bool(body.get("reset", True)), batches=body.get("batches"))
                self._send(200, {"ok": True})
            else:
                self._error(404, "unknown endpoint")
        finally:
            try:
                conn.close()
            except Exception:
                pass


_DB_LOCK = None


def _reset_db():
    path = db.DB_PATH
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(path) + suffix).unlink()
        except FileNotFoundError:
            pass


def serve(host: str = "127.0.0.1", port: int = 8765):
    conn = db.connect()
    conn.close()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"服务已启动：http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    serve(port=port)
