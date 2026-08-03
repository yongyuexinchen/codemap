"""LLM semantic layer: turn per-module symbol summaries into Chinese names.

Pure standard library (sqlite3 + urllib.request + json + hashlib + time) on
purpose — no third-party dependencies. Every function degrades to ``None`` /
``{}`` instead of raising, so the UI keeps working unchanged when no
``DEEPSEEK_API_KEY`` is configured or the network/API misbehaves.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
import urllib.request
from pathlib import Path
from typing import Any

from .config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT

logger = logging.getLogger(__name__)

# Mirrors Bridge.module_of()'s legacy substring rules (the default repo's
# fixed business modules), so summaries bucket files exactly like the
# diagrams. SUPPORT is "everything else" and is filtered in Python.
_LEGACY_LIKE = {
    "AUTH": "%/users%",
    "NEWS": "%/news%",
    "FAV": "%/favorite%",
    "HIST": "%/history%",
}
_LEGACY_SUBSTRINGS = ("/users", "/news", "/favorite", "/history")

# Keep the LLM prompt small: at most this many symbols per module and this
# many characters per symbol line.
_MAX_SYMBOLS = 30
_MAX_PARAMS = 100

_SYSTEM_PROMPT = (
    "你是资深软件架构师。根据模块符号摘要，为每个模块起简洁中文业务名"
    "（2-6 字，业务语义，不要目录名），写一句话职责（≤30 字）。同时判断"
    "项目架构模式（分层/事件驱动/MVC/微服务等）。只输出 JSON，不要其他文字。"
)


# ---- small helpers -----------------------------------------------------

def _repo_root(db_path: str) -> str:
    """Repo root derived from the graph.db location (…/<repo>/.code-review-graph/graph.db)."""
    p = Path(db_path)
    if p.name == "graph.db" and p.parent.name == ".code-review-graph":
        return str(p.parent.parent).replace("\\", "/").rstrip("/") + "/"
    return str(p.parent).replace("\\", "/").rstrip("/") + "/"


def _rel_path(repo_root: str, path: str) -> str:
    """Short display path relative to the repo root (e.g. ``routers/users.py``)."""
    p = (path or "").replace("\\", "/")
    if p.lower().startswith(repo_root.lower()):
        return p[len(repo_root):]
    return p.rsplit("/", 1)[-1]


def _like_for(key: str) -> str:
    """SQL LIKE pattern matching a module's files (default repo: legacy rules)."""
    if key in _LEGACY_LIKE:
        return _LEGACY_LIKE[key]
    if key == "SUPPORT":
        return "%"  # legacy SUPPORT = everything else, filtered in Python
    return f"%/{key.lower()}%"  # derived repos: top-level directory


def _is_legacy_substring(path: str) -> bool:
    """True when path belongs to AUTH/NEWS/FAV/HIST under the legacy rules."""
    p = (path or "").replace("\\", "/")
    return any(sub in p for sub in _LEGACY_SUBSTRINGS)


# ---- step 2a: symbol summaries (DB only, never source files) ----------

def extract_summaries(db_path: str, modules: list[tuple]) -> dict:
    """Bucket DB symbols into per-module summaries.

    ``modules`` is a list of ``(key, name, icon)`` tuples (only ``[0]`` is
    used). Only the ``nodes`` table is read — source files are never opened.
    Returns ``{"KEY": {"files": [...], "symbols": [...]}}``.
    """
    repo_root = _repo_root(db_path)
    summary: dict[str, dict] = {}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for key, _name, _icon in modules:
                rows = con.execute(
                    "SELECT kind, name, params, file_path FROM nodes"
                    " WHERE file_path LIKE ?", (_like_for(key),)
                ).fetchall()
                if key == "SUPPORT":
                    rows = [r for r in rows
                            if not _is_legacy_substring(r[3])]
                files = sorted({_rel_path(repo_root, r[3]) for r in rows})
                symbols: list[str] = []
                for kind, name, params, fp in rows:
                    if len(symbols) >= _MAX_SYMBOLS:
                        break
                    p = " ".join((params or "").split())  # collapse newlines
                    if len(p) > _MAX_PARAMS:
                        p = p[:_MAX_PARAMS].rstrip() + " …"
                    if kind == "File":
                        name = _rel_path(repo_root, fp)  # rel path, not abs
                    symbols.append(f"{kind} {name}{p}")
                summary[key] = {"files": files, "symbols": symbols}
        finally:
            con.close()
    except sqlite3.Error as exc:
        logger.error("extract_summaries failed: %s", exc)
        return {}
    return summary


# ---- step 2b: DeepSeek chat-completions call ---------------------------

def _parse_json(text: str) -> dict | None:
    """json.loads with a ``{first … last}`` slice fallback for prose-wrapped replies."""
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


def call_llm(summaries: dict) -> dict | None:
    """POST symbol summaries to DeepSeek and return the parsed JSON reply.

    The ``LLM_CALL`` print is the cache-hit probe: a cached generate() must
    never reach this function. Returns None on missing key, network error,
    timeout, non-200, or unparseable JSON — callers degrade gracefully.
    """
    print("LLM_CALL")
    if not LLM_API_KEY:
        logger.info("DEEPSEEK_API_KEY not set — skipping LLM call")
        return None
    body = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",
             "content": json.dumps(summaries, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 2000,
        "temperature": 0.3,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {LLM_API_KEY}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning("LLM HTTP %s", resp.status)
                return None
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # network, timeout, JSON — all degrade
        logger.warning("LLM call failed: %r", exc)
        return None
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logger.warning("LLM reply missing choices[0].message.content")
        return None
    return _parse_json(content)


# ---- step 2c: persistence ----------------------------------------------

def save_semantics(db_path: str, result: dict, file_hash: str = "") -> None:
    """Upsert LLM results into the ``semantics`` table (idempotent)."""
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS semantics("
            " id INTEGER PRIMARY KEY,"
            " target_type TEXT, target_key TEXT,"
            " module_name TEXT, summary TEXT, arch_pattern TEXT,"
            " file_hash TEXT, updated_at REAL,"
            " UNIQUE(target_type, target_key))")
        now = time.time()
        for key, info in (result.get("modules") or {}).items():
            con.execute(
                "INSERT OR REPLACE INTO semantics"
                " (target_type, target_key, module_name, summary,"
                "  arch_pattern, file_hash, updated_at)"
                " VALUES ('module', ?, ?, ?, NULL, ?, ?)",
                (key, (info or {}).get("name", ""),
                 (info or {}).get("desc", ""), file_hash, now))
        con.execute(
            "INSERT OR REPLACE INTO semantics"
            " (target_type, target_key, module_name, summary,"
            "  arch_pattern, file_hash, updated_at)"
            " VALUES ('project', 'arch', '', '', ?, ?, ?)",
            (result.get("arch_pattern", ""), file_hash, now))
        con.commit()
    finally:
        con.close()


def get_semantics(db_path: str) -> dict | None:
    """Read the persisted semantics, or None when there is none."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT target_type, target_key, module_name, summary,"
                " arch_pattern FROM semantics").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None  # table missing -> never generated
    if not rows:
        return None
    modules: dict[str, dict] = {}
    arch: str | None = None
    for tt, tk, name, summ, ap in rows:
        if tt == "module":
            modules[tk] = {"name": name, "desc": summ}
        elif tt == "project" and tk == "arch":
            arch = ap
    if not modules and arch is None:
        return None
    return {"modules": modules, "arch_pattern": arch}


def _cached_hashes(db_path: str) -> set[str]:
    """file_hash values stored in the semantics table ('' when empty)."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT DISTINCT file_hash FROM semantics"
                " WHERE target_type = 'module'").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return set()
    return {h for (h,) in rows if h}


# ---- step 2d: cache key ------------------------------------------------

def module_hash(db_path: str, modules: list[tuple]) -> str:
    """Stable hash of every module's file hashes — the cache-invalidation key."""
    parts: list[str] = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for key, _name, _icon in modules:
                rows = con.execute(
                    "SELECT DISTINCT file_hash, file_path FROM nodes"
                    " WHERE file_path LIKE ?", (_like_for(key),)
                ).fetchall()
                if key == "SUPPORT":
                    rows = [r for r in rows
                            if not _is_legacy_substring(r[1])]
                hashes = sorted({h for h, _fp in rows if h})
                parts.append(f"{key}:{','.join(hashes)}")
        finally:
            con.close()
    except sqlite3.Error as exc:
        logger.error("module_hash failed: %s", exc)
        return ""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# ---- step 2e: main entry -----------------------------------------------

def generate(db_path: str, modules: list[tuple],
             force: bool = False) -> dict | None:
    """Cached LLM semantics for ``modules``, or None on any failure.

    Cache hit (persisted file_hash == current module_hash and not force)
    returns immediately — zero LLM calls. A fresh successful call is saved
    with the current hash; a failed call is never cached.
    """
    cur = module_hash(db_path, modules)
    if cur and not force and cur in _cached_hashes(db_path):
        return get_semantics(db_path)  # cache hit: 0 LLM calls
    summaries = extract_summaries(db_path, modules)
    if not summaries:
        return None
    result = call_llm(summaries)
    if result is None:
        return None  # LLM unavailable — never cache a failure
    save_semantics(db_path, result, file_hash=cur)
    return result
