"""few-shot 检索器：random / bm25 / dense / hybrid(RRF) / oracle。

统一接口 retrieve(name, query, pool, k, seed) -> RetrievalResult。
- random：每题确定性（seed:item_id），跨重复种子不同
- bm25：纯 Python Okapi（k1=1.5, b=0.75），中文按字、英文按词
- oracle：用 gold 算子集给池条目打分（Jaccard），诊断"瓶颈在池还是在检索器"
- dense/hybrid：OpenAI 兼容 embeddings API + 磁盘缓存；冒烟不强求
兜底：k > 池大小时返回全池并置 warning，不中断。
"""
from __future__ import annotations

import json
import math
import os
import random
import re
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .datasets import Item

_TOKEN = re.compile(r"[a-zA-Z0-9]+|[一-鿿]")

_STOPWORDS = {
    # 中文虚词与题干高频套话
    "的", "了", "在", "是", "和", "与", "或", "为", "于", "则", "若", "已", "知",
    "设", "求", "值", "多", "少", "什", "么", "吗", "呢", "其", "中", "如", "果",
    "已", "知", "一", "个", "两", "点", "上", "下", "有", "无", "取", "得",
    # 英文
    "the", "a", "an", "of", "to", "in", "is", "are", "what", "which", "how",
    "many", "and", "or", "for", "on", "at", "by",
}


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]


@dataclass
class RetrievalResult:
    items: list[Item]
    warning: str | None = None


# ---------------------------------------------------------------- BM25


class BM25Index:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.doc_tokens = docs
        self.doc_len = [len(d) for d in docs]
        self.avgdl = sum(self.doc_len) / len(self.doc_len) if docs else 0.0
        self.tf = [Counter(d) for d in docs]
        df: Counter = Counter()
        for d in docs:
            df.update(set(d))
        n = len(docs)
        self.idf = {
            term: math.log((n - freq + 0.5) / (freq + 0.5) + 1)
            for term, freq in df.items()
        }

    def scores(self, query_tokens: list[str]) -> list[float]:
        out: list[float] = []
        for i, tf in enumerate(self.tf):
            score = 0.0
            norm = self.k1 * (1 - self.b + self.b * self.doc_len[i] / self.avgdl) if self.avgdl else self.k1
            for term in query_tokens:
                f = tf.get(term, 0)
                if f:
                    score += self.idf.get(term, 0.0) * f * (self.k1 + 1) / (f + norm)
            out.append(score)
        return out


_index_cache: dict[int, BM25Index] = {}


def _bm25_index_for(pool: list[Item]) -> BM25Index:
    key = hash(tuple(it.item_id for it in pool))
    if key not in _index_cache:
        _index_cache[key] = BM25Index([tokenize(it.question) for it in pool])
    return _index_cache[key]


def _rank(scores: list[float], pool: list[Item]) -> list[Item]:
    order = sorted(range(len(pool)), key=lambda i: (-scores[i], pool[i].item_id))
    return [pool[i] for i in order]


# ---------------------------------------------------------------- 各检索器


def _retrieve_random(query: Item, pool: list[Item], seed: int) -> list[Item]:
    order = list(pool)
    random.Random(f"{seed}:{query.item_id}").shuffle(order)
    return order


def _retrieve_bm25(query: Item, pool: list[Item]) -> list[Item]:
    index = _bm25_index_for(pool)
    return _rank(index.scores(tokenize(query.question)), pool)


def _retrieve_oracle(query: Item, pool: list[Item]) -> list[Item]:
    gold_ops = set(query.gold_operators)
    def jaccard(item: Item) -> float:
        ops = set(item.gold_operators)
        union = gold_ops | ops
        return len(gold_ops & ops) / len(union) if union else 1.0
    return _rank([jaccard(it) for it in pool], pool)


def rrf_fuse(rankings: list[list[Item]], rrf_k: int = 60) -> list[Item]:
    """Reciprocal Rank Fusion。weights 默认等权。"""
    scores: dict[str, float] = {}
    items: dict[str, Item] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item.item_id] = scores.get(item.item_id, 0.0) + 1.0 / (rrf_k + rank + 1)
            items[item.item_id] = item
    order = sorted(scores, key=lambda iid: (-scores[iid], iid))
    return [items[iid] for iid in order]


# ---------------------------------------------------------------- dense（API，冒烟不强求）


class DenseError(RuntimeError):
    pass


def _embed_config() -> tuple[str, str, str]:
    """embedding 配置：设置文件（configs/api.json 的 embed_* 键）> 环境变量。"""
    from .settings import load_api_settings

    saved = load_api_settings()
    base_url = (saved.get("embed_base_url") or os.getenv("NL2KE_EMBED_BASE_URL")
                or saved.get("base_url") or os.getenv("OPENAI_BASE_URL"))
    api_key = (
        saved.get("embed_api_key")
        or os.getenv("NL2KE_EMBED_API_KEY")
        or saved.get("api_key")
        or os.getenv("NL2KE_API_KEY")
        or os.getenv("DEEPSEEK_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    model = saved.get("embed_model") or os.getenv("NL2KE_EMBED_MODEL")
    if not (base_url and api_key and model):
        raise DenseError(
            "dense 检索需要 embedding 配置：看板「设置」页填 embed 端点/模型，"
            "或设环境变量 NL2KE_EMBED_BASE_URL / NL2KE_EMBED_API_KEY / NL2KE_EMBED_MODEL"
        )
    return base_url.rstrip("/"), api_key, model


def _embed_texts(texts: list[str], cache_dir: Path | None) -> list[list[float]]:
    base_url, api_key, model = _embed_config()
    cache_dir = cache_dir or Path("data/derived/embed_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    results: list[list[float] | None] = [None] * len(texts)
    todo: list[int] = []
    import hashlib

    def cache_path(text: str) -> Path:
        digest = hashlib.sha256((model + "\x00" + text).encode("utf-8")).hexdigest()
        return cache_dir / f"{digest}.json"

    for i, text in enumerate(texts):
        path = cache_path(text)
        if path.exists():
            results[i] = json.loads(path.read_text(encoding="utf-8"))
        else:
            todo.append(i)
    if todo:
        # embeddings 服务普遍有单批上限（Qwen 约 25 条），分批请求
        entries_all: list = []
        for start in range(0, len(todo), 25):
            batch = todo[start:start + 25]
            payload = {
                "model": model,
                "input": [texts[i] for i in batch],
            }
            request = urllib.request.Request(
                base_url + "/embeddings",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, OSError, ValueError) as exc:
                raise DenseError(f"embeddings 请求失败: {exc}") from exc
            try:
                entries_all.extend(
                    (batch[e["index"]], e["embedding"]) for e in data["data"]
                )
            except (KeyError, IndexError, TypeError) as exc:
                raise DenseError(f"embeddings 响应解析失败: {str(data)[:300]}") from exc
        for i, vec in entries_all:
            results[i] = vec
            cache_path(texts[i]).write_text(
                json.dumps(vec), encoding="utf-8"
            )
    return [r for r in results if r is not None]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _retrieve_dense(query: Item, pool: list[Item]) -> list[Item]:
    texts = [query.question] + [it.question for it in pool]
    vectors = _embed_texts(texts, cache_dir=None)
    qv, doc_vecs = vectors[0], vectors[1:]
    return _rank([_cosine(qv, dv) for dv in doc_vecs], pool)


def _retrieve_hybrid(query: Item, pool: list[Item]) -> list[Item]:
    return rrf_fuse([_retrieve_dense(query, pool), _retrieve_bm25(query, pool)])


# ---------------------------------------------------------------- 统一入口


def retrieve(
    name: str, query: Item, pool: list[Item], k: int, seed: int
) -> RetrievalResult:
    """从 pool 为 query 检索 top-k 示例（有序）。k<=0 返回空；k>池大小兜底全池+warning。"""
    if k <= 0 or not pool:
        return RetrievalResult(items=[])
    warning = None
    if k > len(pool):
        warning = f"pool_smaller_than_k: k={k} > pool={len(pool)}，返回全池"

    if name == "random":
        ranked = _retrieve_random(query, pool, seed)
    elif name == "bm25":
        ranked = _retrieve_bm25(query, pool)
    elif name == "oracle":
        ranked = _retrieve_oracle(query, pool)
    elif name == "dense":
        ranked = _retrieve_dense(query)
    elif name == "hybrid":
        ranked = _retrieve_hybrid(query, pool)
    else:
        raise ValueError(f"未知检索器: {name!r}")
    return RetrievalResult(items=ranked[:k], warning=warning)
