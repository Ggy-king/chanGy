"""RAG 知识库语料接入层：解析 → 清洗 → 切分（父子 chunk）。

设计文档：docs/RAG知识库系统设计.md（§5/§6）

用法：
    python -m rag.ingest clean     # 清洗 + 切分 → data/rag_corpus/clean/corpus.jsonl
    python -m rag.ingest embed     # 子块向量化 → Chroma 入库（data/rag_index/）
    python -m rag.ingest stats     # 查看 clean_log 摘要

产出：
    data/rag_corpus/clean/corpus.jsonl   每行一个父块（一课），含子块列表
    data/rag_corpus/clean/clean_log.md   清洗规则留痕（审计与规则迭代用）
"""
import json
import re
from pathlib import Path

from rag import config

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "rag_corpus" / "raw"
CLEAN_DIR = ROOT / "data" / "rag_corpus" / "clean"

# 108 课的文件名约定：<序号>-<博客原文章ID>-<课号NNN>.md
LESSON_RE = re.compile(r"^(\d+)-[0-9a-z()]+-((?:0\d\d|10[0-8]))\.md$")
COMMENT_MARKER = "**本文评论获取自"

# 子块目标长度（字）：下限以下与相邻段落合并，段落超过上限按句子再切
CHILD_MIN, CHILD_MAX = 200, 500


def parse_raw(path: Path):
    """解析原始 md：头两行元信息 + 正文 + 评论区。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    meta = {"source_file": path.name, "comments": 0, "comments_dropped": 0}
    m = re.match(r"^#\s*\d+\s*-\s*(.+)$", lines[0] if lines else "")
    meta["title"] = m.group(1).strip() if m else path.stem
    m2 = re.search(r"日期：\(([^)]+)\)\s*分类：\[(.*?)\]", "\n".join(lines[:5]))
    if m2:
        meta["date"], meta["category"] = m2.group(1), m2.group(2)

    # 评论区分界
    body_lines, in_comments = [], False
    meta_line_re = re.compile(r"^\s*日期：\(")
    for ln in lines[1:]:
        if meta_line_re.match(ln):
            continue
        if COMMENT_MARKER in ln:
            in_comments = True
            cm = re.search(r"数量：\[?(\d+)", ln)
            meta["comments"] = int(cm.group(1)) if cm else 0
            continue
        (body_lines if not in_comments else []).append(ln)
    body = "\n".join(body_lines)
    return meta, body


def clean_body(body: str, stats: dict) -> str:
    """清洗规则（每条命中都计数，供 clean_log 留痕）。"""
    # R1 剥离「（待续）」等连载标记
    body, n = re.subn(r"[（(]待续[)）]", "", body)
    stats["r1_待续标记"] = stats.get("r1_待续标记", 0) + n
    # R2 剥离 markdown 图片与链接残留（保留链接文字）
    body, n = re.subn(r"!\[[^\]]*\]\([^)]*\)", "", body)
    stats["r2_图片残留"] = stats.get("r2_图片残留", 0) + n
    body = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", body)
    # R3 剥离正文内混入的站务/编辑注记行
    body, n = re.subn(r"^\s*(本文由|以上内容由|原文地址|来自：).*$", "", body, flags=re.M)
    stats["r3_站务注记"] = stats.get("r3_站务注记", 0) + n
    # R4 空白规范化：连续空行合并为一行，行内多空白折叠
    body = re.sub(r"[ \t\u3000]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    stats["r4_空白规范化"] = stats.get("r4_空白规范化", 0) + 1
    return body.strip()


def split_long_paragraph(par: str) -> list[str]:
    """超过 CHILD_MAX 的段落按句子边界再切。"""
    sents = re.split(r"(?<=[。！？；])", par)
    out, buf = [], ""
    for s in sents:
        if buf and len(buf) + len(s) > CHILD_MAX:
            out.append(buf)
            buf = s
        else:
            buf += s
    if buf:
        out.append(buf)
    return out


def make_chunks(body: str) -> list[str]:
    """父子切分的子块：贪心合并相邻段落到 [CHILD_MIN, CHILD_MAX] 区间。"""
    paras = [p.strip() for p in body.split("\n") if len(p.strip()) >= 20]
    chunks, buf = [], ""
    for p in paras:
        pieces = [p] if len(p) <= CHILD_MAX else split_long_paragraph(p)
        for piece in pieces:
            if buf and len(buf) + len(piece) > CHILD_MAX and len(buf) >= CHILD_MIN:
                chunks.append(buf)
                buf = piece
            else:
                buf = (buf + "\n" + piece).strip() if buf else piece
    if buf:
        if len(buf) < CHILD_MIN and chunks:  # 尾部小碎块并入前一块
            chunks[-1] += "\n" + buf
        else:
            chunks.append(buf)
    return chunks


def run_clean():
    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    stats: dict = {}
    skipped, records = [], []

    for f in sorted(RAW_DIR.glob("*.md")):
        m = LESSON_RE.match(f.name)
        if not m:
            skipped.append(f.name)
            continue
        meta, body = parse_raw(f)
        stats["评论区整段剔除_课数"] = stats.get("评论区整段剔除_课数", 0) + 1
        stats["剔除评论条数"] = stats.get("剔除评论条数", 0) + meta["comments"]
        meta.update(lesson=int(m.group(2)), seq=m.group(1))
        body = clean_body(body, stats)
        if len(body) < 100:
            skipped.append(f.name + "(清洗后过短)")
            continue
        chunks = make_chunks(body)
        records.append({
            "lesson": meta["lesson"], "title": meta["title"],
            "date": meta.get("date", ""), "category": meta.get("category", ""),
            "source_file": meta["source_file"], "text": body,
            "chunks": [{"chunk_id": f"L{meta['lesson']:03d}-{i}", "text": c}
                       for i, c in enumerate(chunks)],
        })

    records.sort(key=lambda r: r["lesson"])
    out = CLEAN_DIR / "corpus.jsonl"
    with out.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    total_chunks = sum(len(r["chunks"]) for r in records)
    total_chars = sum(len(r["text"]) for r in records)
    log = [
        "# RAG 语料清洗留痕（clean_log）",
        "",
        f"- 语料来源：新浪博客缠中说禅原文存档（stockServ/chzhshch-108-plus，MIT License），含博主原文与网友评论",
        f"- 入库：{len(records)} 课（父块），子块 {total_chunks} 个，正文合计 {total_chars} 字",
        f"- 非课程文件不入库（{len(skipped)} 个）：{', '.join(skipped) or '无'}",
        "",
        "## 清洗规则命中统计",
        "",
    ]
    for k, v in stats.items():
        log.append(f"- {k}: {v}")
    log += ["", "## 规则说明", "",
            "- R1 剥离「（待续）」连载标记；R2 剥离图片/链接残留（保留链接文字）；",
            "- R3 剥离站务/编辑注记行；R4 空白规范化（连续空行合并、行内空白折叠）；",
            "- 评论区（网友回复）整段剔除，不做切分；", "",
            "## 子块切分口径", "",
            f"- 段落 <20 字并入邻段；贪心合并至 [{CHILD_MIN},{CHILD_MAX}] 字；超长段落按句子边界再切。", ""]
    (CLEAN_DIR / "clean_log.md").write_text("\n".join(log), encoding="utf-8")

    print(f"父块 {len(records)} 课 | 子块 {total_chunks} | 合计 {total_chars} 字")
    print(f"跳过: {skipped or '无'}")
    print(f"输出: {out} / {CLEAN_DIR/'clean_log.md'}")


def run_embed():
    """子块向量化并写入 Chroma 持久库（重建式：先清空 collection）。"""
    records = [json.loads(l) for l in config.CORPUS_JSONL.open(encoding="utf-8")]
    docs, metas, ids = [], [], []
    for r in records:
        for c in r["chunks"]:
            docs.append(c["text"])
            ids.append(c["chunk_id"])
            metas.append({"lesson": r["lesson"], "title": r["title"], "date": r["date"]})

    _embed_and_write(config.COLLECTION, docs, metas, ids)


def run_embed_private():
    """私有阅览室入库（二期）：体系疑问清单结论 / 周复盘回答与行动项 / 手写口径术语。

    语料全是结构化 JSON/MD，无需清洗管线；重建式（复盘数据每天变，重跑即最新）。
    """
    docs, metas, ids = [], [], []
    reviews = json.loads(config.REVIEWS_JSON.read_text(encoding="utf-8")) if config.REVIEWS_JSON.exists() else {}

    for q in reviews.get("questions") or []:
        text = (q.get("text") or "").strip()
        note = (q.get("note") if isinstance(q.get("note"), str) else "").strip()
        if text:
            docs.append(text)
            ids.append(f"P-q{q.get('id')}-main")
            metas.append({"kind": "体系疑问", "date": q.get("created", ""),
                          "title": text[:40], "status": q.get("status", "")})
        if note:
            docs.append(note)
            ids.append(f"P-q{q.get('id')}-note")
            metas.append({"kind": "疑问结论", "date": q.get("created", ""),
                          "title": text[:40], "status": q.get("status", "")})

    for wk, w in (reviews.get("weekly") or {}).items():
        for key, kind in (("answers", "三问回答"), ("actions", "行动项")):
            v = w.get(key)
            if isinstance(v, dict):
                v = "；".join(str(x) for x in v.values() if x)
            if isinstance(v, str) and v.strip():
                docs.append(v.strip())
                ids.append(f"P-w{wk}-{key}")
                metas.append({"kind": kind, "date": wk, "title": f"周复盘 {wk}", "status": ""})

    if not config.PRIVATE_GLOSSARY.exists():
        tpl = config.PRIVATE_GLOSSARY.parent / "glossary.template.md"
        if tpl.exists():
            config.PRIVATE_GLOSSARY.write_text(tpl.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"[rag] 已从模板生成私有术语表：{config.PRIVATE_GLOSSARY}（请编辑成您自己的口径）")
    if config.PRIVATE_GLOSSARY.exists():
        gtext = config.PRIVATE_GLOSSARY.read_text(encoding="utf-8")
        for i, chunk in enumerate(make_chunks(gtext)):
            docs.append(chunk)
            ids.append(f"P-gloss-{i}")
            metas.append({"kind": "口径术语", "date": "", "title": "系统口径术语表", "status": ""})

    if not docs:
        print("私有语料为空（reviews.json 无疑问/周数据，且无 glossary.md），跳过")
        return

    _embed_and_write(config.COLLECTION_PRIVATE, docs, metas, ids)


def _embed_and_write(collection_name, docs, metas, ids):
    """加载模型 → 向量化 → 重建式写入指定 collection。公共/私有共用。"""
    import chromadb
    from sentence_transformers import SentenceTransformer

    print(f"加载模型 {config.EMBED_MODEL} ...")
    model = SentenceTransformer(config.EMBED_MODEL)
    print(f"向量化 {len(docs)} 条 ...")
    vecs = model.encode(docs, batch_size=32, normalize_embeddings=True, show_progress_bar=False)

    client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
    try:
        client.delete_collection(collection_name)
    except Exception:
        pass
    col = client.get_or_create_collection(collection_name, metadata={"hnsw:space": "cosine"})
    B = 128
    for i in range(0, len(docs), B):
        col.add(ids=ids[i:i + B], documents=docs[i:i + B],
                embeddings=vecs[i:i + B].tolist(), metadatas=metas[i:i + B])
    print(f"入库完成：{collection_name} {col.count()} 条 → {config.CHROMA_DIR}")


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "clean"
    if cmd == "clean":
        run_clean()
    elif cmd == "embed":
        run_embed()
    elif cmd == "embed-private":
        run_embed_private()
    elif cmd == "stats":
        print((CLEAN_DIR / "clean_log.md").read_text(encoding="utf-8"))
    else:
        print(__doc__)
