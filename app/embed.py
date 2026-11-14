"""本地可运行的文本向量：中文分词 + 词/字符 n-gram TF-IDF 稀疏向量。

不依赖在线大模型，也不需下载模型文件：只用 Python 标准库和本地 jieba
（未安装时自动退回字符 2-gram）。向量 L2 归一化，点积即余弦相似度。
稀疏表示使关键词可以由原始特征直接读出，便于解释"为什么放在一起"。
"""

from __future__ import annotations

import math
import re

_WORD = re.compile(r"[一-鿿]{2,}|[a-zA-Z]{2,}")
_STOP = {
    "我们", "你们", "他们", "这个", "那个", "就是", "还是", "但是", "因为", "所以",
    "已经", "一直", "现在", "然后", "可以", "没有", "什么", "怎么", "为什么", "一下",
    "希望", "麻烦", "请问", "问题", "情况", "客户", "老师", "您好", "你好", "谢谢",
    "目前", "感觉", "时候", "东西", "之前", "之后", "以及", "而且", "如果", "还有",
    "显示", "收到", "要求", "需要", "进行", "相关", "这个", "一个", "不是", "就是",
    "的话", "但是", "还是", "办法", "告诉", "知道", "觉得", "看看", "请问", "处理",
    "the", "and", "for", "with", "that", "this",
}
_NUM = re.compile(r"^\d+(\.\d+)?$")
_PUNCT = re.compile(r"[^0-9a-zA-Z一-鿿]+")
_PLAIN = re.compile(r"^[0-9a-zA-Z一-鿿]+$")

try:  # 本地中文分词；缺失时自动退回字符 n-gram
    import jieba  # type: ignore

    jieba.setLogLevel(20)
    _HAS_JIEBA = True
except Exception:  # pragma: no cover - 环境差异
    jieba = None
    _HAS_JIEBA = False


def tokenize(text: str) -> list[str]:
    if _HAS_JIEBA:
        raw = [t.strip() for t in jieba.lcut(text) if t.strip()]
    else:
        raw = _WORD.findall(text)
    out: list[str] = []
    for tok in raw:
        low = tok.lower()
        if low in _STOP:
            continue
        if len(tok) == 1 and "一" <= tok <= "鿿":
            continue
        if not _PLAIN.match(tok):
            tok = _PUNCT.sub("", tok)
            if not tok:
                continue
        if _NUM.match(tok):
            continue
        out.append(tok.lower())
    return out


def features(text: str) -> dict[str, float]:
    """词特征为主，字符 bigram 只做很轻的补充。

    词权重高，关键词才解释得清楚；字符 n-gram 权重压到 0.08，
    只用来兜住"退款/退钱"这类同义写法，不让高频片段主导聚类。
    没有本地分词器时，字符 bigram 承担主要的语义近似作用。
    """
    toks = tokenize(text)
    feats: dict[str, float] = {}
    for tok in toks:
        key = f"w::{tok}"
        feats[key] = feats.get(key, 0.0) + 1.0
    chars = re.sub(r"\s+", "", text)
    char_weight = 0.5 if _HAS_JIEBA else 1.0
    for i in range(len(chars) - 2):
        key = f"c3::{chars[i:i + 3]}"
        feats[key] = feats.get(key, 0.0) + char_weight
    return feats


def vectorize(text: str, idf: dict[str, float], default_idf: float = 1.0) -> dict[str, float]:
    feats = features(text)
    vec = {
        key: (1.0 + math.log(tf)) * idf.get(key, default_idf)
        for key, tf in feats.items()
    }
    norm = math.sqrt(sum(v * v for v in vec.values()))
    if norm <= 0:
        return {}
    return {k: v / norm for k, v in vec.items()}


def fit_idf(texts: list[str]) -> dict[str, float]:
    """按文档频率计算 IDF；只保留有区分度的特征。

    单篇文档里出现的高频词（"退款""客服"）和只在 1 篇出现的碎片都压不住噪声，
    因此额外用"出现在 >=2 篇"作为候选门槛——由调用方的 `fit_idf`+`vectorize`
    共同实现：IR 里的 df 由全部语料统计，噪声特征自然被加权压低。
    """
    df: dict[str, int] = {}
    for text in texts:
        for key in features(text):
            df[key] = df.get(key, 0) + 1
    n = max(1, len(texts))
    return {k: math.log((1 + n) / (1 + v)) + 1.0 for k, v in df.items()}


def dot(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(k, 0.0) for k, v in a.items())


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """两向量均已 L2 归一化，点积即余弦。"""
    return dot(a, b)


def centroid(vectors: list[dict[str, float]]) -> dict[str, float]:
    acc: dict[str, float] = {}
    for vec in vectors:
        for k, v in vec.items():
            acc[k] = acc.get(k, 0.0) + v
    norm = math.sqrt(sum(v * v for v in acc.values()))
    if norm <= 0:
        return {}
    return {k: v / norm for k, v in acc.items()}


def mean_pairwise(vectors: list[dict[str, float]]) -> float:
    if len(vectors) < 2:
        return 1.0
    total, count = 0.0, 0
    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            total += cosine(vectors[i], vectors[j])
            count += 1
    return total / count


def explain_terms(
    doc_vectors: list[dict[str, float]],
    centroid_vec: dict[str, float],
    limit: int = 6,
) -> list[dict]:
    """挑出最能代表该簇、且在簇内多数文档出现的**完整词**，作为可读关键词。

    只取词特征（w::）：字符 n-gram 片段（",希"、"到的"）对人不友好，也会
    让关键词看起来像原文里没有的东西。返回 [{term, weight, docs, kind}]，
    关键词只是归类线索，不能当作原文事实。
    """
    support: dict[str, int] = {}
    for vec in doc_vectors:
        for key in vec:
            if key.startswith("w::"):
                support[key] = support.get(key, 0) + 1
    if not doc_vectors:
        return []
    min_support = max(2, math.ceil(len(doc_vectors) * 0.5))
    ranked = sorted(
        (
            (key, centroid_vec.get(key, 0.0), support.get(key, 0))
            for key in support
            if support[key] >= min_support
        ),
        key=lambda item: (item[2], item[1]),
        reverse=True,
    )
    picked: list[dict] = []
    seen_text: set[str] = set()
    for key, weight, docs in ranked:
        term = key.split("::", 1)[1]
        if len(term) < 2:
            continue
        # 长词优先：已被更长的词包含就跳过
        if any(term in other for other in seen_text):
            continue
        seen_text.add(term)
        picked.append({"term": term, "weight": round(weight, 4), "docs": docs, "kind": "词"})
        if len(picked) >= limit:
            break
    return picked
