#!/usr/bin/env python3
"""
Leo MCP Helpers - Utilidades compartidas

Módulo refactorizado para leo_mcp_server.py v10.0
"""

import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime
from typing import List, Optional


HERMES_DIR = os.path.expanduser("~/.hermes")
SKILLS_FILE = os.path.join(HERMES_DIR, "skills.json")
DEBUG_LOG = os.path.join(HERMES_DIR, "mcp_debug.log")

# ── Cache ───────────────────────────────────────────────────────────────────
_CACHE_DB = os.path.join(HERMES_DIR, "leo_cache.db")
_CACHE_TTL_MINUTES = int(os.environ.get("LEO_CACHE_TTL", "30"))
_CACHE_BYPASS_SKILLS = {"/data_analyst", "/market-research-intensive"}


def _cache_key(skill_path: str, prompt: str) -> str:
    return hashlib.sha256(f"{skill_path}::{prompt}".encode()).hexdigest()[:64]


def _cache_init():
    try:
        with sqlite3.connect(_CACHE_DB) as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, response TEXT, created_at INTEGER)"
            )
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_cache_created ON cache(created_at)"
            )
    except Exception:
        pass


def _cache_get(key: str) -> Optional[str]:
    try:
        ttl = _CACHE_TTL_MINUTES * 60
        with sqlite3.connect(_CACHE_DB) as con:
            cur = con.execute(
                "SELECT response FROM cache WHERE key = ? AND created_at > ?",
                (key, int(time.time()) - ttl),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _cache_set(key: str, response: str):
    try:
        with sqlite3.connect(_CACHE_DB) as con:
            con.execute(
                "INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                (key, response, int(time.time())),
            )
    except Exception:
        pass


def _cache_clear_expired():
    try:
        ttl = _CACHE_TTL_MINUTES * 60
        with sqlite3.connect(_CACHE_DB) as con:
            con.execute(
                "DELETE FROM cache WHERE created_at <= ?", (int(time.time()) - ttl,)
            )
    except Exception:
        pass


# ── Metrics ───────────────────────────────────────────────────────────────────
_METRICS_DB = os.path.join(HERMES_DIR, "leo_metrics.db")


def _metrics_init():
    try:
        with sqlite3.connect(_METRICS_DB) as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS requests "
                "(ts INTEGER, skill TEXT, prompt_len INTEGER, duration_ms INTEGER, status TEXT)"
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_requests_skill ON requests(skill)")
    except Exception:
        pass


def _metrics_log(skill: str, prompt_len: int, duration_ms: int, status: str):
    try:
        with sqlite3.connect(_METRICS_DB) as con:
            con.execute(
                "INSERT INTO requests VALUES (?, ?, ?, ?, ?)",
                (int(time.time()), skill, prompt_len, duration_ms, status),
            )
    except Exception:
        pass


def _metrics_get() -> List[dict]:
    try:
        with sqlite3.connect(_METRICS_DB) as con:
            con.row_factory = sqlite3.Row
            cur = con.execute("SELECT * FROM requests ORDER BY ts DESC LIMIT 100")
            return [dict(row) for row in cur.fetchall()]
    except Exception:
        return []


# ── Conversation History ───────────────────────────────────────────────────────
_CONV_DB = os.path.join(HERMES_DIR, "leo_conversations.db")


def _conv_init():
    try:
        with sqlite3.connect(_CONV_DB) as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS conversations ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  ts INTEGER NOT NULL,"
                "  skill TEXT NOT NULL,"
                "  uuid TEXT NOT NULL,"
                "  prompt_preview TEXT"
                ")"
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_conv_ts ON conversations(ts DESC)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_conv_uuid ON conversations(uuid)")
    except Exception:
        pass


def _conv_log(skill: str, uuid: str, prompt: str):
    try:
        preview = prompt[:200] if prompt else ""
        with sqlite3.connect(_CONV_DB) as con:
            con.execute(
                "INSERT INTO conversations (ts, skill, uuid, prompt_preview) VALUES (?, ?, ?, ?)",
                (int(time.time()), skill, uuid, preview),
            )
    except Exception:
        pass


def _conv_get_recent(limit: int = 20) -> List[dict]:
    try:
        with sqlite3.connect(_CONV_DB) as con:
            con.row_factory = sqlite3.Row
            cur = con.execute(
                "SELECT ts, skill, uuid, prompt_preview FROM conversations ORDER BY ts DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]
    except Exception:
        return []


# ── Lock para serialización ───────────────────────────────────────────────────
import threading


class AsyncBraveLock:
    """Async-safe wrapper for threading lock."""

    def __init__(self, lock: threading.Lock = None):
        self._lock = lock or threading.Lock()

    async def __aenter__(self):
        import asyncio

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._lock.acquire)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._lock.release()
        return False


# Instance global para usar en leo_mcp_server.py
_brave_lock = AsyncBraveLock()


# ── Logging ───────────────────────────────────────────────────────────────────
def _log_json(level: str, event_type: str, **kwargs):
    """Structured JSON logging for observability."""
    try:
        from datetime import datetime
        log_entry = {
            "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{datetime.utcnow().microsecond // 1000:03d}Z",
            "level": level,
            "event_type": event_type,
            **kwargs,
        }
        with open(DEBUG_LOG, "a", encoding="utf-8") as dbg:
            dbg.write(json.dumps(log_entry) + "\n")
    except Exception:
        pass


# ── Helpers ──────────────────────────────────────────────────────────────────
def _to_str(x) -> str:
    """Coerce ANY value to str."""
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, bytes):
        return x.decode("utf-8", "ignore")
    if isinstance(x, (list, tuple)):
        return "\n".join(_to_str(i) for i in x)
    if isinstance(x, dict):
        if "text" in x:
            return _to_str(x["text"])
        if "content" in x:
            return _to_str(x["content"])
        return str(x)
    return str(x)

def _clean_response(raw) -> str:
    """Strip shell noise, return only Leo's answer."""
    raw = _to_str(raw)
    if not isinstance(raw, str):
        raw = str(raw)

    marker = "--- Respuesta de Leo"
    if marker in raw:
        parts = raw.split(marker, 1)
        answer = parts[-1] if len(parts) > 1 else raw

        for end_marker in [
            ">>> [LOG] Listo!",
            ">>> ¡Listo!",
            "Limpieza completada.",
            ">>> [LOG] Cleaning up UI",
            ">>> [LOG] Cleanup done.",
        ]:
            if end_marker in answer:
                answer = answer.split(end_marker, 1)[0]

        answer = _to_str(answer).lstrip()
        if answer.startswith("("):
            nl = answer.find("\n")
            if nl != -1:
                answer = answer[nl + 1 :]
        return answer.strip()

    lines = raw.splitlines()
    clean = [
        l
        for l in lines
        if not l.startswith(">>>")
        and not l.startswith("✅")
        and not l.startswith("⚠️")
        and not l.startswith("❌")
        and not l.startswith("--- ")
    ]
    return "\n".join(clean).strip()


def _load_skills() -> dict:
    if not os.path.exists(SKILLS_FILE):
        return {}
    try:
        with open(SKILLS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {s["id"]: s for s in data.get("skills", [])}
    except Exception:
        return {}


def _detect_file_required(text: str) -> List[str]:
    """Extract filenames from FILE_REQUIRED responses."""
    text = _to_str(text)
    pattern = r"FILE_REQUIRED:\s*(.+)"
    matches = re.findall(pattern, text)
    return [m.strip() for m in matches]


def _detect_copy_block(text: str):
    """Extract COPY_FOR_QWEN block."""
    text = _to_str(text)
    for open_m in ["```COPY_FOR_QWEN", "```copy_for_qwen"]:
        if open_m in text:
            start = text.index(open_m) + len(open_m)
            end = text.find("```", start)
            if end != -1:
                return text[start:end].strip()
    if "COPY_FOR_QWEN" in text:
        after = text[text.index("COPY_FOR_QWEN") :]
        bs = after.find("```")
        if bs != -1:
            inner_start = after.find("\n", bs) + 1
            inner_end = after.find("```", inner_start)
            if inner_end != -1:
                return after[inner_start:inner_end].strip()
    return None


def _detect_plan_block(text: str):
    """Extract ===COPY_START=== ... ===COPY_END=== block."""
    text = _to_str(text)
    start_m = "===COPY_START==="
    end_m = "===COPY_END==="
    if start_m in text and end_m in text:
        start = text.index(start_m) + len(start_m)
        end = text.index(end_m, start)
        return text[start:end].strip()
    return None


def _detect_truncation(text: str) -> bool:
    """
    Detect if a response appears truncated (cut off mid-sentence or mid-code).

    Heuristics (at least one must match):
    1. Unclosed code fence: odd number of ``` markers
    2. Unclosed markdown: unclosed **bold** or *italic* markers
    3. Trailing ellipsis: text ends with '...' (Leo's truncation signal)
    4. Truncated list: last meaningful line is a list item with no closing text
    5. Ends mid-sentence: last line lacks sentence-ending punctuation
       AND ends with alphanumeric (not a URL, not a file path)

    Args:
        text: The response text to check.

    Returns:
        True if the response likely truncated, False otherwise.
    """
    text = _to_str(text).strip()
    if not text:
        return False

    # 1. Unclosed code fence
    fence_count = text.count("```")
    if fence_count % 2 != 0:
        return True

    # 2. Unclosed markdown (bold/italic)
    for marker in ["**", "*"]:
        if marker in text:
            count = text.count(marker)
            if count % 2 != 0:
                return True

    # 3. Trailing ellipsis (Leo sometimes uses this as truncation signal)
    if text.rstrip().endswith("..."):
        return True

    # 4. Truncated list: last non-empty line is a bullet/numbered item
    #    with no concluding text after it
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if lines:
        last = lines[-1]
        is_list_item = (
            last.startswith("- ")
            or last.startswith("* ")
            or last.startswith("+ ")
            or (len(last) > 2 and last[0].isdigit() and last[1:].startswith(". "))
        )
        if is_list_item:
            # If the last meaningful line is a list item and there's no
            # text after the last newline that looks like a conclusion,
            # it's likely truncated
            return True

    # 5. Ends mid-sentence: last line lacks sentence-ending punctuation
    last_line = lines[-1] if lines else ""
    if last_line and len(last_line) > 10:
        sentence_enders = {".", "!", "?", ":", ")", "]", "}", '"', "'"}
        if not any(last_line.endswith(c) for c in sentence_enders):
            # Exclude lines that end with a URL or file path
            if "://" in last_line or last_line.startswith(("/", "./")):
                return False
            if last_line[-1].isalnum():
                return True

    return False


def _build_file_context(files: dict) -> str:
    """Build a file context preamble to prepend to prompts."""
    if not files:
        return ""
    lines = ["=== PROVIDED FILE CONTEXT (read carefully before responding) ===\n"]
    for filename, content in files.items():
        lines.append(f"--- FILE: {filename} ---")
        lines.append(_to_str(content).strip())
        lines.append(f"--- END: {filename} ---\n")
    lines.append("=== END FILE CONTEXT ===\n")
    return "\n".join(lines)


# Initialize DBs on module load
_cache_init()
_metrics_init()
_conv_init()