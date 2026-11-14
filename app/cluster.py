"""本地聚类：球面 k-means（余弦）+ 层次凝聚做主题数估计 + 噪点/离群判定。

全部在进程内跑，不调用任何在线服务。聚类结果以 {doc_id: cluster_index} 返回，
簇编号仅用于本次训练内部，跨批次历史只依靠内容对应（见 pipeline.py）。
"""

from __future__ import annotations


import random

from .embed import centroid, cosine, dot

SEED = 20261114


def _kmeans_once(
    vectors: list[dict[str, float]], k: int, rng: random.Random, iters: int = 60
) -> tuple[list[int], list[dict[str, float]]]:
    n = len(vectors)
    if n == 0 or k <= 0:
        return [], []
    k = min(k, n)
    centers: list[dict[str, float]] = [vectors[rng.randrange(n)]]
    while len(centers) < k:
        dists = []
        for vec in vectors:
            best = max(cosine(vec, c) for c in centers)
            dists.append(max(0.0, 1.0 - best))
        total = sum(dists)
        if total <= 1e-12:
            centers.append(vectors[rng.randrange(n)])
            continue
        pick = rng.random() * total
        acc = 0.0
        for vec, d in zip(vectors, dists):
            acc += d
            if acc >= pick:
                centers.append(vec)
                break
    labels = [-1] * n
    for _ in range(iters):
        changed = False
        for i, vec in enumerate(vectors):
            sims = [cosine(vec, c) for c in centers]
            best = max(range(len(sims)), key=lambda j: sims[j])
            if labels[i] != best:
                labels[i] = best
                changed = True
        new_centers = []
        for j in range(len(centers)):
            members = [vectors[i] for i in range(n) if labels[i] == j]
            new_centers.append(centroid(members) if members else centers[j])
        centers = new_centers
        if not changed:
            break
    return labels, centers


def inertia(vectors: list[dict[str, float]], labels: list[int], centers: list[dict[str, float]]) -> float:
    total = 0.0
    for vec, lab in zip(vectors, labels):
        total += 1.0 - cosine(vec, centers[lab])
    return total


def average_link(
    vectors: list[dict[str, float]], threshold: float, max_clusters: int = 12,
    secondary_threshold: float | None = None, secondary_min_size: int = 2,
) -> dict:
    """平均连接层次聚类。

    比 k-means 更适合"条数不均、彼此相似度差异很大"的工单文本：
    当最近两簇的平均相似度跌破阈值就停止合并，不被簇数绑死。
    返回值结构与 `cluster()` 一致，便于上层无感替换。

    `secondary_threshold` 是第二轮"补合并"阈值：有些同一主题的工单只共享
    "退款/到账/银行卡"这类高频词，平均相似度会卡在阈值下方一点点而被切碎。
    第二轮只合并两个成员都不少于 `secondary_min_size` 的簇，避免把一条
    偶然相似的离题工单（单成员簇）吸进来；单成员簇宁可在页面上单列、
    由人工合并，也不硬塞。
    """
    n = len(vectors)
    if n == 0:
        return {"labels": [], "k": 0, "centers": [], "cohesion": {}}
    clusters: dict[int, list[int]] = {i: [i] for i in range(n)}
    # 预先算两两相似度，再按簇平均
    sim_cache: dict[tuple[int, int], float] = {}

    def pair_sim(a: list[int], b: list[int]) -> float:
        key = (min(a[0], b[0]), max(a[0], b[0]))
        # 小簇缓存粒度不够，直接算；条数不大，复杂度可接受
        total = 0.0
        for i in a:
            for j in b:
                total += cosine(vectors[i], vectors[j])
        return total / (len(a) * len(b))

    while len(clusters) > 1:
        best_sim, best_pair = -1.0, None
        keys = list(clusters)
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                sim = pair_sim(clusters[keys[i]], clusters[keys[j]])
                if sim > best_sim:
                    best_sim, best_pair = sim, (keys[i], keys[j])
        if best_pair is None or best_sim < threshold:
            break
        x, y = best_pair
        clusters[x] = clusters[x] + clusters[y]
        del clusters[y]
        if len(clusters) <= max_clusters:
            # 达到上限仍可继续合并，这里不强制停止；阈值才是主要判据
            pass

    # 第二轮：把被切碎的同一主题补回来，但只动“两边都至少 2 条”的簇
    if secondary_threshold is not None:
        while len(clusters) > 1:
            best_sim, best_pair = -1.0, None
            keys = list(clusters)
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    a, b = clusters[keys[i]], clusters[keys[j]]
                    if len(a) < secondary_min_size or len(b) < secondary_min_size:
                        continue
                    sim = pair_sim(a, b)
                    if sim > best_sim:
                        best_sim, best_pair = sim, (keys[i], keys[j])
            if best_pair is None or best_sim < secondary_threshold:
                break
            x, y = best_pair
            clusters[x] = clusters[x] + clusters[y]
            del clusters[y]

    labels = [-1] * n
    centers: list[dict[str, float]] = []
    sizes: list[int] = []
    for idx, members in enumerate(clusters.values()):
        center = centroid([vectors[i] for i in members])
        centers.append(center)
        sizes.append(len(members))
        for i in members:
            labels[i] = idx
    cohesion = {}
    for j, center in enumerate(centers):
        members = [vectors[i] for i in range(n) if labels[i] == j]
        sims = [cosine(v, center) for v in members]
        cohesion[j] = round(sum(sims) / len(sims), 4)
    return {"labels": labels, "k": len(centers), "centers": centers, "cohesion": cohesion, "sizes": sizes}


def cluster(
    vectors: list[dict[str, float]],
    max_k: int = 8,
    threshold: float = 0.12,
    rng_seed: int = SEED,
    merge_threshold: float | None = 0.10,
) -> dict:
    """先做层次聚类得到主题，再对过大的簇补一轮 k-means。

    即便条数不多、主题彼此不均衡，也能得到可读的分组；簇编号只是本次训练的
    内部编号，跨批次对应由上层按质心相似度做二部匹配。
    `threshold` 越低，合并越宽松、主题越少；由设置里的 cluster_threshold 控制。
    """
    n = len(vectors)
    if n == 0:
        return {"labels": [], "k": 0, "centers": [], "cohesion": {}}
    if n == 1:
        return {"labels": [0], "k": 1, "centers": [vectors[0]], "cohesion": {0: 1.0}}
    base = average_link(
        vectors, threshold=threshold, max_clusters=max_k,
        secondary_threshold=merge_threshold if merge_threshold and merge_threshold < threshold else None,
    )
    # 太大的簇（超过 40% 且 > 9 条）再切一次，避免单个主题吞掉一半数据
    labels = base["labels"]
    centers = base["centers"]
    cohesion = base["cohesion"]
    for j, size in enumerate(base.get("sizes", [])):
        members = [i for i, l in enumerate(labels) if l == j]
        if size > max(9, int(n * 0.4)) and size > 2:
            sub_labels, sub_centers = _kmeans_once([vectors[i] for i in members], 2, random.Random(rng_seed))
            for local, member in enumerate(members):
                if sub_labels[local] == 1:
                    labels[member] = len(centers) + len(sub_centers) - 1
            centers.extend(sub_centers[-1:])
            # 原有的第一个子簇沿用 j 的质心
            centers[j] = sub_centers[0]
    # 重新编号，保证连续
    remap: dict[int, int] = {}
    for old in labels:
        if old not in remap:
            remap[old] = len(remap)
    labels = [remap[l] for l in labels]
    new_centers = [centers[old] for old in remap]
    cohesion = {}
    for j in range(len(new_centers)):
        members = [vectors[i] for i in range(n) if labels[i] == j]
        sims = [cosine(v, new_centers[j]) for v in members]
        cohesion[j] = round(sum(sims) / len(sims), 4)
    return {"labels": labels, "k": len(new_centers), "centers": new_centers, "cohesion": cohesion}


def representative_index(vectors: list[dict[str, float]], center: dict[str, float]) -> int:
    """与质心最接近的成员作为代表工单。"""
    best_idx, best_sim = 0, -1.0
    for i, vec in enumerate(vectors):
        sim = dot(vec, center)
        if sim > best_sim:
            best_idx, best_sim = i, sim
    return best_idx
