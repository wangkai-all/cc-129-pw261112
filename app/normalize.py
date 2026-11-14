"""工单文本整理：引用回复剥离、重复投递识别、签名/客套语清理。

所有规则都可通过 `CleanRules` 调整；页面上修改规则后重新跑同一套函数即可，
原文与清理结果的对照在 `clean_document` 的返回结构里逐段保留。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, asdict
from typing import Iterable


@dataclass
class CleanRules:
    # 引用回复
    strip_quote_headers: bool = True      # 去掉 "在 xxx 写道：" 之类引导行
    strip_quoted_lines: bool = True       # 去掉以 > 开头的引用行
    strip_reply_chain: bool = True        # 去掉 "-----原始邮件-----" 之后的部分
    # 签名 / 客套
    strip_signature: bool = True          # 去掉 "-- " 分隔的签名块
    strip_signature_words: bool = True    # 去掉 "此致 敬礼" 等落款套话
    strip_greeting: bool = True           # 去掉 "您好，亲爱的客服："
    # 机器生成
    strip_auto_footer: bool = True        # 去掉 "本邮件由系统自动发送" 等
    strip_case_noise: bool = True         # 去掉工单号/时间戳/电话/邮箱等模板噪声
    # 去重
    near_duplicate_threshold: float = 0.8  # 加权字符 3-gram 余弦；>= 阈值视为重复投递
    # 短文本
    min_chars: int = 12                   # 清理后少于该字数进入"短文本"待整理区
    min_tokens: int = 4

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict | None) -> "CleanRules":
        base = cls()
        if not raw:
            return base
        values = base.to_dict()
        for key in values:
            if key not in raw:
                continue
            value = raw[key]
            if isinstance(values[key], bool):
                values[key] = bool(value)
            elif isinstance(values[key], int):
                values[key] = max(0, int(value))
            elif isinstance(values[key], float):
                values[key] = min(0.999, max(0.0, float(value)))
        return cls(**values)


# ---------------------------------------------------------------------------
# 正则
# ---------------------------------------------------------------------------

_QUOTE_HEADER = re.compile(
    r"^\s*(在\s*[^\n]{0,60}?写道\s*[:：]?|[-—]{2,}\s*(原始邮件|原始消息|转发的邮件|Original Message)[-—]{0,}\s*|"
    r"发件人\s*[:：].{0,40}|From\s*[:：].{0,40}|On .{0,80}wrote\s*:)\s*$",
    re.IGNORECASE,
)
_QUOTE_MARK = re.compile(r"^\s*(>\s*|＞\s*)")
_SIG_SEPARATOR = re.compile(r"^\s*--\s*$")
_SIGNATURE_LINE = re.compile(
    r"^\s*(此致|敬礼|顺颂商祺|祝好|谢谢[!！。]?|感谢[!！。]?|"
    r"客服中心|客户服务中心|技术支持|运营团队|某某公司|XX公司)\s*[!！。]?\s*$"
)
_GREETING = re.compile(
    r"^\s*(尊敬的|亲爱的)?\s*(客服(人员|同学|中心|小助手)?|您好|你好|有关负责部门|相关部门|后台|平台|工作人员)"
    r"[^\n。！？]{0,24}[:：，,！!]\s*",
)
_AUTO_FOOTER = re.compile(
    r"(本邮件由系统自动发送|此邮件为系统自动发送|请勿直接回复本邮件|"
    r"本消息由.{0,12}自动推送|系统自动生成，?请勿)"
)
_FORWARD_HEADER = re.compile(
    r"^\s*(转发[:：]|转自[:：]|Fwd\s*:|Forwarded message|.{0,20}转发给我的)\s*$",
    re.IGNORECASE,
)
# 落款：单独一行的姓名 / 姓名+称谓 / 联系人标签。只在文末生效，避免误伤正文。
_SIGN_OFF_LINE = re.compile(
    r"^\s*(发件人|寄件人|投诉人|联系人|发送时间|收件人)\s*[:：].{0,40}$|"
    r"^\s*[一-鿿]{2,3}(先生|女士|小姐|同学|老师)?\s*([|｜·].{0,30})?\s*$"
)
_SIGN_OFF_TAIL = re.compile(
    r"^\s*(订单号|单号|手机号|电话|邮箱|附件)\s*[:：]\s*[^\s。！？]{0,24}\s*$"
)
# 模板噪声：工单号、UUID、手机号、邮箱、日期时间、URL
_CASE_NO = re.compile(
    r"(\b(?:TK|WO|TICKET|ORDER|工单|订单)[-_ ]?\d{4,}\b|"
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|"
    r"\b1[3-9]\d{9}\b|\b[\w.+-]+@[\w-]+\.[\w.]+\b|"
    r"\b20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}日?\s*\d{1,2}:\d{2}(?::\d{2})?\b|"
    r"\b20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}日\b)",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://\S+|www\.\S+")
# 模板噪声被删掉后留下的空标签（"订单号 ，" / "发送时间:"）
_DANGLING_LABEL = re.compile(
    r"(订单号|单号|工单号|手机号|电话|邮箱|发送时间|发件人|收件人|联系人|附件)\s*[:：]?\s*(?=[，,。；;、\s]|$)"
)
_DATE_PREFIX = re.compile(r"^\s*【?\s*20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}[^】]{0,8}】?\s*")

_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("　", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------

@dataclass
class CleanResult:
    clean_text: str
    segments: list[dict]          # {text, kind, original} —— 原文与处理方式的逐段对照
    removed_chars: int
    original_chars: int
    dup_key: str                  # 完全相同的归一化键
    dup_text: str                 # 用于近似相似度比较的文本

    def to_dict(self) -> dict:
        return {
            "clean_text": self.clean_text,
            "segments": self.segments,
            "removed_chars": self.removed_chars,
            "original_chars": self.original_chars,
            "dup_key": self.dup_key,
            "dup_text": self.dup_text,
        }


def _segment_kinds(line: str, rules: CleanRules) -> str | None:
    """返回该行应被整行剔除的原因；None 表示保留。"""
    stripped = line.strip()
    if not stripped:
        return "blank"
    if rules.strip_quote_headers and _QUOTE_HEADER.match(line):
        return "quote_header"
    if rules.strip_quoted_lines and _QUOTE_MARK.match(line):
        return "quoted_line"
    if rules.strip_reply_chain and _FORWARD_HEADER.match(line):
        return "forward_header"
    if rules.strip_signature and _SIG_SEPARATOR.match(line):
        return "signature"
    if rules.strip_signature_words and _SIGNATURE_LINE.match(line):
        return "signature"
    if rules.strip_auto_footer and _AUTO_FOOTER.search(line):
        return "auto_footer"
    return None


def _strip_sign_off_tail(text: str, segments: list[dict]) -> str:
    """剥掉文末的落款行（姓名、联系人、订单号尾巴），最多三行。

    只从后往前删，并保证剩余正文仍有一定长度，避免把短工单整条删空。
    """
    lines = text.split("\n")
    removed = 0
    while lines and removed < 3:
        tail = lines[-1].strip()
        if not tail:
            lines.pop()
            continue
        if _SIGN_OFF_LINE.match(tail) or _SIGN_OFF_TAIL.match(tail):
            segments.append({"text": tail, "kind": "signature", "original": False})
            lines.pop()
            removed += 1
            continue
        break
    kept_len = sum(len(l) for l in lines)
    if kept_len < 8:
        return text
    return "\n".join(lines).strip()


def clean_document(raw: str, rules: CleanRules, subject: str = "") -> CleanResult:
    """把一条原始工单拆成"保留 / 剔除"两栏，返回清理文本与对照明细。"""
    original = _norm(raw or "")
    subject = _norm(subject or "")
    segments: list[dict] = []
    kept: list[str] = []

    lines = original.split("\n")
    drop_rest_reason: str | None = None
    for idx, line in enumerate(lines):
        if drop_rest_reason:
            segments.append({"text": line, "kind": drop_rest_reason, "original": True})
            continue
        kind = _segment_kinds(line, rules)
        if kind == "quote_header":
            # 引用引导行之后就是被转发的原信；整段剥离，避免同一条投诉被计成两次。
            # 注意：只有确实跟了引用行 / 转发行时才丢掉后文，否则只去掉这一行。
            segments.append({"text": line, "kind": kind, "original": True})
            lookahead = lines[idx + 1 : idx + 4]
            if any(_QUOTE_MARK.match(l) for l in lookahead):
                drop_rest_reason = "quoted_block"
            elif any(_FORWARD_HEADER.match(l) for l in lookahead):
                drop_rest_reason = "reply_chain"
            continue
        if kind == "forward_header":
            segments.append({"text": line, "kind": kind, "original": True})
            drop_rest_reason = "reply_chain"
            continue
        if kind:
            segments.append({"text": line, "kind": kind, "original": True})
            continue
        segments.append({"text": line, "kind": "kept", "original": True})
        kept.append(line)

    text = "\n".join(kept)

    if rules.strip_greeting:
        new = _GREETING.sub("", text)
        if new != text:
            segments.append({"text": new, "kind": "greeting", "original": False})
            text = new

    if rules.strip_case_noise:
        before = text
        text = _DATE_PREFIX.sub("", text)
        text = _CASE_NO.sub(" ", text)
        text = _URL.sub(" ", text)
        while True:
            new = _DANGLING_LABEL.sub(" ", text)
            if new == text:
                break
            text = new
        if text != before:
            segments.append({"text": "", "kind": "case_noise", "original": False})

    if rules.strip_signature_words:
        text = _strip_sign_off_tail(text, segments)

    # 分句清理：逐句判断签名/客套/自动页脚，保留有信息量的句子
    sentences = [s for s in _SENT_SPLIT.split(text) if s.strip()]
    kept_sentences: list[str] = []
    for sent in sentences:
        reason = None
        if rules.strip_signature_words and _SIGNATURE_LINE.match(sent.strip()):
            reason = "signature"
        elif rules.strip_auto_footer and _AUTO_FOOTER.search(sent):
            reason = "auto_footer"
        if reason:
            segments.append({"text": sent.strip(), "kind": reason, "original": False})
        else:
            kept_sentences.append(sent.strip())
    text = " ".join(dict.fromkeys(s for s in kept_sentences if s))
    text = re.sub(r"\s+", " ", text).strip(" ，,。")

    # 用于相似度比对的归一化键：去掉标点、空白与模板噪声
    dup_text = _CASE_NO.sub("", unicodedata.normalize("NFKC", text or subject))
    dup_text = re.sub(r"[\s\W_]+", "", dup_text).lower()
    # 去掉常见过场词，避免"客服/页面/麻烦"之类的常用语撑高相似度
    for filler in ("客服", "页面", "麻烦", "尽快", "希望", "您好", "你好", "谢谢", "显示", "已经", "还是", "但是"):
        dup_text = dup_text.replace(filler, "")
    dup_key = hashlib.sha1(dup_text.encode("utf-8")).hexdigest()[:16]

    return CleanResult(
        clean_text=text,
        segments=segments,
        removed_chars=max(0, len(original) - len(text)),
        original_chars=len(original),
        dup_key=dup_key,
        dup_text=dup_text,
    )


# ---------------------------------------------------------------------------
# 近似重复
# ---------------------------------------------------------------------------

def shingles(text: str, n: int = 3) -> set[str]:
    chars = _compact(text)
    if len(chars) <= n:
        return {chars} if chars else set()
    return {chars[i : i + n] for i in range(len(chars) - n + 1)}


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def shingle_counts(text: str, n: int = 3) -> dict[str, int]:
    chars = _compact(text)
    if len(chars) <= n:
        return {chars: 1} if chars else {}
    counts: dict[str, int] = {}
    for i in range(len(chars) - n + 1):
        key = chars[i : i + n]
        counts[key] = counts.get(key, 0) + 1
    return counts


def cosine_counts(a: dict[str, int], b: dict[str, int]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    if dot <= 0:
        return 0.0
    na = sum(v * v for v in a.values()) ** 0.5
    nb = sum(v * v for v in b.values()) ** 0.5
    return dot / (na * nb)


def containment(a: str, b: str) -> float:
    """较短文本的 3-gram 被较长文本覆盖的比例；识别"回复里整段带了原文"。"""
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    small, large = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    return len(small & large) / len(small)


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def dedupe(docs: Iterable[dict], threshold: float) -> list[dict]:
    """输入 [{id, dup_text, support, ...}]，标注重复投递关系。

    判定同时看两件事：
      1) 加权 3-gram 余弦 >= 阈值（同一条投诉换个说法再次提交）
      2) 较短一方大部分 3-gram 被较长一方包含（回复里整段复制了原文）
    这样"引用回复 + 签名"不会把同一条投诉数成两条，也就不会凭空造出趋势上涨。
    极短文本（< 10 字）不参与近似判定，避免"打不开""在吗"这类短语互相吞并。
    """
    items = list(docs)
    counts = {d["id"]: shingle_counts(d["dup_text"]) for d in items}
    groups: list[dict] = []
    doc_meta: dict[str, dict] = {}

    for doc in items:
        target = None
        too_short = len(_compact(doc["dup_text"])) < 10
        for group in groups:
            lead = group["lead"]
            if too_short or len(_compact(lead["dup_text"])) < 10:
                score = 0.0
            else:
                cos = cosine_counts(counts[doc["id"]], counts[lead["id"]])
                # 包含关系要求较长一方也达到最小长度，否则短语会误判
                cover = containment(doc["dup_text"], lead["dup_text"])
                score = max(cos, cover)
            if score >= threshold:
                target = group
                doc_meta[doc["id"]] = {
                    "canonical": False,
                    "duplicate_of": lead["id"],
                    "duplicate_similarity": round(score, 3),
                }
                group["members"].append(doc["id"])
                group["support"] += doc.get("support", 1)
                break
        if target is None:
            groups.append({"lead": doc, "members": [doc["id"]], "support": doc.get("support", 1)})
            doc_meta[doc["id"]] = {"canonical": True, "duplicate_of": None, "duplicate_similarity": 1.0}

    lead_support = {g["lead"]["id"]: g["support"] for g in groups}
    out = []
    for doc in items:
        meta = doc_meta[doc["id"]]
        merged = dict(doc)
        merged.update(meta)
        merged["support"] = lead_support.get(doc["id"], 1)
        out.append(merged)
    return out


# ---------------------------------------------------------------------------
# 分句
# ---------------------------------------------------------------------------

def split_sentences(text: str) -> list[str]:
    text = _norm(text)
    if not text:
        return []
    parts = [p.strip(" ，,；;") for p in _SENT_SPLIT.split(text)]
    return [p for p in parts if p]
