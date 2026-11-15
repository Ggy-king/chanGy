"""RAG 知识库检索层：查询改写（术语映射）→ 双路召回（公共原文 + 私有阅览室）→ 带出处结果。

设计文档：docs/RAG知识库系统设计.md（§7/§8）

用法：
    python -m rag.retrieve "三类买卖点的定义是什么"
"""
import json
import threading

import chromadb

from rag import config

_INDEX_LOCK = threading.Lock()
_index_ready = False

# 术语映射：本系统口径术语 → 缠论原文术语展开（query rewrite，§8）
# 命中左侧词时，检索串追加右侧展开词，避免黑话搜不到原文
TERM_MAP = {
    "金卡": "区间套 背驰 精确定位",
    "A类": "第一类买卖点",
    "B类": "第二类买卖点",
    "C类": "第三类买卖点",
    "一类买点": "第一类买点",
    "二类买点": "第二类买点",
    "三类买点": "第三类买点",
    "三类买卖点": "第三类买卖点",
    "一买": "第一类买点",
    "二买": "第二类买点",
    "三买": "第三类买点",
    "一卖": "第一类卖点",
    "二卖": "第二类卖点",
    "三卖": "第三类卖点",
    "顺大逆小": "大级别趋势 小级别买卖点 级别",
    "成笔": "笔 分型 线段",
    "中枢震荡": "中枢 震荡 区间",
    "背驰确认": "背驰 盘整背驰 趋势背驰",
    "定位": "区间套 显微镜 精确",
    "级别漂移": "级别 递归 扩展",
}

_model = None
_corpus_by_lesson = None


def _load_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(config.EMBED_MODEL)
    return _model


def _load_corpus():
    global _corpus_by_lesson
    if _corpus_by_lesson is None:
        _corpus_by_lesson = {
            r["lesson"]: r
            for r in map(json.loads, config.CORPUS_JSONL.open(encoding="utf-8"))
        }
    return _corpus_by_lesson


def rewrite(query: str) -> str:
    """术语展开：命中映射词则把展开词追加到查询尾（不改写原句，只增广）。"""
    extra = [v for k, v in TERM_MAP.items() if k in query]
    return f"{query} {' '.join(extra)}".strip()


def _ensure_index():
    """索引缺失自动重建（新机器克隆后零手动步骤）：公共层缺语料先清洗，再入库；
    私有层为空也自动入库。进程内加锁防并发重建。"""
    global _index_ready
    if _index_ready:
        return
    with _INDEX_LOCK:
        if _index_ready:
            return
        from rag import ingest
        client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
        pub = client.get_or_create_collection(config.COLLECTION)
        if pub.count() == 0:
            print("[rag] 公共索引为空，自动构建（首次需下载 embedding 模型，请耐心等待）...")
            if not config.CORPUS_JSONL.exists():
                ingest.run_clean()
            ingest.run_embed()
        pri = client.get_or_create_collection(config.COLLECTION_PRIVATE)
        if pri.count() == 0:
            print("[rag] 私有阅览室为空，自动入库...")
            ingest.run_embed_private()
        _index_ready = True


def _query_collection(col, qv, n):
    if col.count() == 0:
        return {"documents": [[]], "metadatas": [[]], "distances": [[]]}
    return col.query(query_embeddings=[qv.tolist()], n_results=min(n, col.count()),
                     include=["documents", "metadatas", "distances"])


def search(query: str, top_k: int = config.TOP_K) -> list[dict]:
    """双路召回：公共层（缠论原文）+ 私有层（体系疑问/周复盘/口径），按相似度合并。

    公共层每条：{lesson, title, source, snippet, score, text(整课原文), kind='原文'}
    私有层每条：{lesson=None, title, source, snippet, score, text='', kind}
    """
    _ensure_index()
    rewritten = rewrite(query)
    model = _load_model()
    qv = model.encode([config.QUERY_PREFIX + rewritten],
                      normalize_embeddings=True)[0]

    client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
    corpus = _load_corpus()
    cand = []
    for coll_name, is_private in ((config.COLLECTION, False), (config.COLLECTION_PRIVATE, True)):
        res = _query_collection(client.get_or_create_collection(coll_name), qv, top_k * 2)
        for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
            cand.append({"doc": doc, "meta": meta, "score": round(1 - dist, 4),
                         "private": is_private})
    cand.sort(key=lambda x: -x["score"])

    out, seen_lessons = [], set()
    for c in cand:
        if c["private"]:
            out.append({
                "lesson": None, "title": c["meta"].get("kind", "私有"),
                "source": f"{c['meta'].get('kind', '私有')}"
                          + (f"（{c['meta'].get('date')}）" if c["meta"].get("date") else ""),
                "snippet": c["doc"], "score": c["score"], "text": "",
                "kind": c["meta"].get("kind", "私有"),
            })
        else:
            lesson = c["meta"]["lesson"]
            if lesson in seen_lessons:
                continue
            seen_lessons.add(lesson)
            parent = corpus.get(lesson, {})
            out.append({
                "lesson": lesson, "title": c["meta"]["title"],
                "source": f"缠论第{lesson}课", "snippet": c["doc"],
                "score": c["score"], "text": parent.get("text", ""), "kind": "原文",
            })
        if len(out) >= top_k:
            break
    return out


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "三类买卖点的定义"
    print(f"[改写后] {rewrite(q)}\n")
    for hit in search(q):
        print(f"{hit['source']} | {hit['title']} | score={hit['score']}")
        print(f"  片段: {hit['snippet'][:120]}...")
        print()
