"""Small semantic search over the policy documents.

Each Markdown file is split on its `## ` sections, so every hit can be cited as
`doc-id › Section`. Chunks are embedded once with the Gemini embedding model and
cached on disk; at ~50 chunks a brute-force cosine search is all we need. In
production this whole module is replaced by Vertex AI Search (see docs/PRODUCTION_NOTE.md).
"""

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from google import genai
from google.genai import types

from . import config


@dataclass
class Chunk:
    doc_id: str
    doc_title: str
    section: str
    text: str

    @property
    def citation(self) -> str:
        return f"{self.doc_id} › {self.section}"


def load_chunks() -> list[Chunk]:
    chunks = []
    for path in sorted(config.POLICIES_DIR.glob("*.md")):
        lines = path.read_text().splitlines()
        title = lines[0].lstrip("# ").strip()
        section, body = "Overview", []

        def flush():
            text = "\n".join(body).strip()
            if text:
                chunks.append(Chunk(path.stem, title, section, text))

        for line in lines[1:]:
            if line.startswith("## "):
                flush()
                section, body = line[3:].strip(), []
            else:
                body.append(line)
        flush()
    return chunks


def _embed(client: genai.Client, texts: list[str], task_type: str) -> np.ndarray:
    result = client.models.embed_content(
        model=config.EMBEDDING_MODEL,
        contents=texts,
        config=types.EmbedContentConfig(task_type=task_type),
    )
    vectors = np.array([e.values for e in result.embeddings], dtype=np.float32)
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


class PolicyIndex:
    def __init__(self):
        self.client = genai.Client()
        self.chunks = load_chunks()
        # Embed title + section + body so short sections keep their context.
        texts = [f"{c.doc_title} — {c.section}\n{c.text}" for c in self.chunks]
        self.vectors = self._load_or_build(texts)

    def _load_or_build(self, texts: list[str]) -> np.ndarray:
        digest = hashlib.sha256((config.EMBEDDING_MODEL + "".join(texts)).encode()).hexdigest()[:16]
        cache_file = config.CACHE_DIR / f"policy_embeddings_{digest}.json"
        if cache_file.exists():
            return np.array(json.loads(cache_file.read_text()), dtype=np.float32)
        vectors = _embed(self.client, texts, "RETRIEVAL_DOCUMENT")
        config.CACHE_DIR.mkdir(exist_ok=True)
        cache_file.write_text(json.dumps(vectors.tolist()))
        return vectors

    def search(self, query: str, k: int = 4) -> list[tuple[Chunk, float]]:
        q = _embed(self.client, [query], "RETRIEVAL_QUERY")[0]
        scores = self.vectors @ q
        top = np.argsort(-scores)[:k]
        return [(self.chunks[i], float(scores[i])) for i in top]


@lru_cache(maxsize=1)
def get_index() -> PolicyIndex:
    return PolicyIndex()
