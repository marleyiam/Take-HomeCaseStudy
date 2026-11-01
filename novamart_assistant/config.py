"""Central configuration. Everything overridable through environment variables / .env."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Gemini via AI Studio by default. Set GOOGLE_GENAI_USE_VERTEXAI=TRUE (+ project/location) to run on Vertex AI.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")

MODEL = os.getenv("NOVAMART_MODEL", "gemini-3.5-flash")
# A stronger model than the agent, so the judge is not grading its own mistakes.
JUDGE_MODEL = os.getenv("NOVAMART_JUDGE_MODEL", "gemini-3.1-pro-preview")
EMBEDDING_MODEL = os.getenv("NOVAMART_EMBEDDING_MODEL", "gemini-embedding-001")

POLICIES_DIR = ROOT / "data" / "policies"
ORDERS_PATH = ROOT / "data" / "orders.json"
ESCALATIONS_PATH = Path(os.getenv("NOVAMART_ESCALATIONS_PATH", ROOT / "data" / "escalations.jsonl"))
CACHE_DIR = ROOT / ".cache"
