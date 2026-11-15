"""RAG 知识库集中配置（模型/路径/参数），embedding 可在此切换。"""
import os

# 模型加载默认离线（EMBED_MODEL 已缓存在本地，避免每次启动联网检查导致卡死）。
# 首次更换 EMBED_MODEL 时：置 "0" 联网下载（国内网络需 HF_ENDPOINT=https://hf-mirror.com），下载完再改回。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS_JSONL = ROOT / "data" / "rag_corpus" / "clean" / "corpus.jsonl"
RAG_INDEX_DIR = ROOT / "data" / "rag_index"

# Embedding 模型（本地 BGE；换模型只改这里 + 重跑 ingest embed）
# 备选：BAAI/bge-m3（更强/多语言）、Qwen/Qwen3-Embedding-0.6B
EMBED_MODEL = "BAAI/bge-small-zh-v1.5"
EMBED_DIM = 512

# Chroma 持久化
CHROMA_DIR = RAG_INDEX_DIR / "chroma"
COLLECTION = "chanlun"            # 公共阅览室：缠论 108 课原文
COLLECTION_PRIVATE = "private"    # 私有阅览室：体系疑问结论 / 周复盘行动 / 口径术语

# 私有语料源（结构化数据，免清洗）
REVIEWS_JSON = ROOT / "data" / "reviews.json"
PRIVATE_GLOSSARY = ROOT / "data" / "rag_corpus" / "private" / "glossary.md"

# bge v1.5 官方查询前缀（短查询检索场景）
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："

# 检索返回条数
TOP_K = 5
