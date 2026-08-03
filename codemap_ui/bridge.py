"""Python<->JS bridge exposed to the page as ``window.pywebview.api``.

All public methods must return JSON-serializable values: pywebview encodes
the return value and resolves the JS-side promise with it.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .config import MIN_HEIGHT, MIN_WIDTH, get_db_path

logger = logging.getLogger(__name__)

# Columns surfaced to the page. Keep payloads small but enough to render.
NODE_COLS = (
    "id", "kind", "name", "qualified_name", "file_path",
    "line_start", "line_end", "language", "parent_name", "params",
    "return_type", "is_test", "community_id",
)
EDGE_COLS = (
    "id", "kind", "source_qualified", "target_qualified",
    "file_path", "line", "confidence", "confidence_tier",
)

# ---- business-module split (default repo: same rules as make_arch_demo3.py)
MODULES = [
    ("AUTH", "认证模块", "🔐"),
    ("NEWS", "新闻模块", "📰"),
    ("FAV", "收藏模块", "⭐"),
    ("HIST", "历史模块", "🕘"),
    ("SUPPORT", "支撑模块", "🔧"),
]
MODULE_KEYS = [k for k, _, _ in MODULES]
MOD_NAME = {k: n for k, n, _ in MODULES}
MOD_ICON = {k: i for k, _, i in MODULES}
REPO_ROOT = "E:/BaiduNetdiskDownload/code/toutiao_backend/"
# Directory that contains the `code_review_graph` package (the venv runs the
# incremental `update` subcommand from here).
CODEMAP_ROOT = str(Path(__file__).resolve().parent.parent)
# Edge kinds that count as a dependency for the macro diagram.
DEP_KINDS = ("CALLS", "REFERENCES", "IMPORTS_FROM")

# Icon palette for modules auto-derived from a loaded repo's top-level
# directories (the default repo keeps its fixed MODULES instead).
ICON_POOL = ("📦", "🧩", "⚙️", "🔧", "📁", "🗂️", "📚", "🧰", "⚡", "🛠️")
# Top-level directories that never become a module.
HIDDEN_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__",
    "dist", "build", ".idea", ".vscode", ".code-review-graph", ".tox",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "htmlcov", ".eggs",
}


class Bridge:
    """js_api for the CodeMap window (``window.pywebview.api``)."""

    def __init__(self) -> None:
        self._window: Any = None
        # Detached micro (class-diagram) window, wired up by app.py.
        self._micro_window: Any = None
        self._micro_bridge: Any = None
        # Incremental-refresh state: last snapshot of the nodes table and the
        # change set computed on the last refresh (see `refresh`).
        self._last_snapshot: dict | None = None
        self._change: dict | None = None
        self._refreshing = False
        # Loaded-repo state. Defaults to the legacy hardcoded repo so the
        # app behaves exactly as before; `load_repo()` swaps these in.
        self.repo_root = REPO_ROOT
        self.db_path = get_db_path()
        self.modules: list[tuple[str, str, str]] = list(MODULES)
        self._legacy = True          # default repo: substring module rules
        self._comp_map: dict[str, str] | None = None  # derived: dir -> key
        self._module_keys = [k for k, _, _ in self.modules]
        self._mod_name = {k: n for k, n, _ in self.modules}
        self._mod_icon = {k: i for k, _, i in self.modules}

    # ---- repo-aware path/module helpers ---------------------------------

    def _rel_of(self, path: str) -> str:
        """Path relative to the loaded repo root (case-insensitive match)."""
        p = (path or "").replace("\\", "/")
        root = self.repo_root.replace("\\", "/").rstrip("/") + "/"
        if p.lower().startswith(root.lower()):
            return p[len(root):]
        return p.rsplit("/", 1)[-1]

    def rel_path(self, path: str) -> str:
        """Short display path relative to the repo root (e.g. ``models/users.py``)."""
        return self._rel_of(path)

    def module_of(self, path: str) -> str:
        """Map a file path to a business module.

        Legacy (default repo) keeps the make_arch_demo3 substring rules
        byte-for-byte. Derived repos look up the path's first-level directory
        (case-insensitive) in ``_comp_map``; root-level files belong to the
        ``ROOT`` module.
        """
        p = (path or "").replace("\\", "/")
        if self._legacy:
            for key, mod in (("/users", "AUTH"), ("/news", "NEWS"),
                             ("/favorite", "FAV"), ("/history", "HIST")):
                if key in p:
                    return mod
            return "SUPPORT"
        rel = self._rel_of(p)
        if "/" in rel:
            return self._comp_map.get(rel.split("/", 1)[0].lower(), "ROOT")
        return "ROOT"

    def load_repo(self, path: str) -> dict:
        """Point the app at a different repo without editing any code.

        ``path`` is a filesystem path to a git repo. If it has no
        ``.code-review-graph/graph.db`` yet, CRG's ``build`` subcommand runs
        first (non-git directories fail there and the error is returned).
        The module list is derived from the File nodes' top-level directories
        — except when loading the default repo, which restores the fixed
        business modules. Returns the fresh macro data so the page can
        rebuild itself in one round-trip.
        """
        if self._refreshing:
            return {"status": "busy", "message": "load in progress"}
        raw = (path or "").strip().strip('"').strip("'").strip()
        if not raw:
            return {"error": "路径为空"}
        repo = str(Path(raw).expanduser()).replace("\\", "/").rstrip("/") + "/"
        if not os.path.isdir(repo):
            return {"error": f"目录不存在: {repo}"}

        db = repo + ".code-review-graph/graph.db"
        if not os.path.isfile(db):
            self._refreshing = True
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "code_review_graph", "build",
                     "--repo", repo, "-q"],
                    cwd=CODEMAP_ROOT, capture_output=True, text=True,
                    timeout=600,
                )
            except FileNotFoundError:
                return {"error": "code_review_graph 包未找到（请确认在 venv 内启动）"}
            except subprocess.TimeoutExpired:
                return {"error": "CRG build 超时（600s）"}
            finally:
                self._refreshing = False
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip()[-400:]
                logger.error("CRG build failed (rc=%s): %s",
                             proc.returncode, tail)
                return {"error": "CRG build 失败: " + (tail or "见日志")}

        self.repo_root = repo
        self.db_path = db
        if repo.lower() == REPO_ROOT.lower():
            self.modules = list(MODULES)
            self._legacy = True
            self._comp_map = None
        else:
            self.modules, self._comp_map = self._derive_modules(db, repo)
            self._legacy = False
        self._module_keys = [k for k, _, _ in self.modules]
        self._mod_name = {k: n for k, n, _ in self.modules}
        self._mod_icon = {k: i for k, _, i in self.modules}
        self._last_snapshot = None
        self._change = None
        self._inherit_state(self._micro_bridge)
        return {
            "status": "ok",
            "repo": self.repo_root,
            "db": self.db_path,
            "modules": [{"key": k, "name": n, "icon": i}
                        for k, n, i in self.modules],
            "mtime": self._db_mtime(),
            "macro": self.get_macro(),
        }

    def _derive_modules(self, db: str, repo: str
                        ) -> tuple[list[tuple[str, str, str]], dict[str, str]]:
        """Derive modules from the File nodes' top-level directories.

        Returns (modules, comp_map): modules is a list of ``(key, name,
        icon)`` tuples (most files first, ``ROOT`` for repo-root files) and
        comp_map maps a lowercased top-level directory to its module key.
        """
        root = repo.rstrip("/") + "/"
        top_dirs: dict[str, tuple[str, int]] = {}
        root_files = 0
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "SELECT file_path FROM nodes WHERE kind = 'File'"
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error as exc:
            logger.error("load_repo: cannot read %s: %s", db, exc)
            return list(MODULES), {}
        for (fp,) in rows:
            p = (fp or "").replace("\\", "/")
            rel = p[len(root):] if p.lower().startswith(root.lower()) else p
            if "/" in rel:
                top = rel.split("/", 1)[0]
                low = top.lower()
                if low and low not in HIDDEN_DIRS:
                    name, n = top_dirs.get(low, (top, 0))
                    top_dirs[low] = (name, n + 1)
            elif rel:
                root_files += 1

        modules: list[tuple[str, str, str]] = []
        comp_map: dict[str, str] = {}
        used: set[str] = set()

        def make_key(name: str) -> str:
            key = "".join(c if c.isalnum() or c == "_" else "_"
                          for c in name.upper())
            if key and key[0].isdigit():
                key = "M" + key
            if not key:
                key = "M"
            base, i = key, 2
            while key in used:
                key = f"{base}_{i}"
                i += 1
            used.add(key)
            return key

        if root_files:
            modules.append(("ROOT", "根目录", "📁"))
            used.add("ROOT")
        ranked = sorted(top_dirs.items(), key=lambda kv: (-kv[1][1], kv[0]))
        for i, (low, (name, _n)) in enumerate(ranked[:12]):
            modules.append((make_key(name), name, ICON_POOL[i % len(ICON_POOL)]))
            comp_map[low] = modules[-1][0]
        if not modules:
            modules.append(("ROOT", "根目录", "📁"))
        # The ROOT bucket (files directly under the repo root) reads best at
        # the end, after the named directories.
        if len(modules) > 1 and modules[0][0] == "ROOT":
            modules.append(modules.pop(0))
        return modules, comp_map

    def _inherit_state(self, other: "Bridge | None") -> None:
        """Copy the loaded-repo state onto the detached micro bridge."""
        if other is None:
            return
        other.repo_root = self.repo_root
        other.db_path = self.db_path
        other.modules = list(self.modules)
        other._legacy = self._legacy
        other._comp_map = dict(self._comp_map) if self._comp_map else None
        other._module_keys = list(self._module_keys)
        other._mod_name = dict(self._mod_name)
        other._mod_icon = dict(self._mod_icon)
        other._last_snapshot = None
        other._change = self._change
        # If the micro window's module vanished in the new repo, fall back
        # to the first module so pop_micro keeps working.
        if (getattr(other, "_module_key", None) is not None
                and other._module_key not in other._module_keys):
            other._module_key = (other._module_keys[0]
                                 if other._module_keys else None)

    def get_graph(self) -> dict:
        """Read nodes/edges from graph.db and return them as JSON data."""
        db_path = self.db_path
        nodes: list[dict] = []
        edges: list[dict] = []
        try:
            # Open read-only so the CRG daemon can keep writing concurrently.
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                con.row_factory = sqlite3.Row
                nodes = [dict(r) for r in con.execute(
                    f"SELECT {', '.join(NODE_COLS)} FROM nodes")]
                edges = [dict(r) for r in con.execute(
                    f"SELECT {', '.join(EDGE_COLS)} FROM edges")]
            finally:
                con.close()
        except sqlite3.Error as exc:
            logger.error("Failed to read graph.db (%s): %s", db_path, exc)
            return {"error": str(exc), "nodes": [], "edges": []}
        return {"nodes": nodes, "edges": edges}

    def refresh(self) -> dict:
        """Incrementally re-scan the repo and return the change set.

        Runs CRG's ``update`` subcommand (git-diff based, re-parses only the
        files that changed since the graph was last built), then compares the
        ``nodes`` table before/after:

          * ``added``   - qualified_names present after but not before
          * ``removed`` - qualified_names present before but not after
          * ``modified`` - qualified_names present on both sides whose
                           ``file_hash`` changed (CRG hashes per file, so
                           every node of an edited file is reported)

        The change set is stored on the bridge so ``get_micro``/``get_macro``
        can attach highlight info to the diagrams. Polling uses ``stat()``'s
        graph.db mtime to trigger this method automatically.
        """
        if self._refreshing:
            return {"status": "busy", "message": "refresh already running"}
        self._refreshing = True
        try:
            before = self._snapshot_nodes()
            if self._last_snapshot is None:
                self._last_snapshot = before
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "code_review_graph", "update",
                     "--repo", self.repo_root, "-q"],
                    cwd=CODEMAP_ROOT, capture_output=True, text=True,
                    timeout=300,
                )
            except FileNotFoundError:
                return {"status": "error",
                        "error": "code_review_graph package not found"}
            except subprocess.TimeoutExpired:
                return {"status": "error",
                        "error": "CRG update timed out after 300s"}
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip()[-400:]
                logger.error("CRG update failed (rc=%s): %s",
                             proc.returncode, tail)
                return {"status": "error",
                        "error": "CRG update failed: " + (tail or "see logs")}

            after = self._snapshot_nodes()
            diff = self._diff_snapshots(self._last_snapshot, after)
            self._last_snapshot = after
            changed = bool(diff["by_qn"]["added"] or diff["by_qn"]["modified"]
                           or diff["by_qn"]["removed"])
            # Only replace the change set when new changes were found; a
            # no-op refresh (redundant click / external db touch) must not
            # wipe the highlights already shown in the diagrams.
            if changed:
                self._change = diff["by_qn"]
            self._sync_micro_window()
            cs = diff["serialized"]
            return {
                "status": "ok",
                "changed": changed,
                "change_set": cs,
                "db_mtime": self._db_mtime(),
                "message": (
                    f"新增 {len(cs['added'])} · 修改 {len(cs['modified'])}"
                    f" · 删除 {len(cs['removed'])}"
                    if changed else "无变更"),
            }
        finally:
            self._refreshing = False

    def stat(self) -> dict:
        """graph.db mtime + paths, for the JS polling loop (every 2 s)."""
        try:
            return {"mtime": os.path.getmtime(self.db_path),
                    "db": self.db_path, "repo": self.repo_root,
                    "exists": True}
        except OSError:
            return {"mtime": None, "db": self.db_path,
                    "repo": self.repo_root, "exists": False}

    def _db_mtime(self) -> float | None:
        try:
            return os.path.getmtime(self.db_path)
        except OSError:
            return None

    def _snapshot_nodes(self) -> dict:
        """Read the nodes table as {qualified_name: info} (read-only)."""
        db_path = self.db_path
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT qualified_name, kind, parent_name, file_path,"
                " file_hash FROM nodes").fetchall()
        finally:
            con.close()
        return {r[0]: {"kind": r[1], "parent": r[2], "file": r[3],
                       "hash": r[4]} for r in rows}

    def _diff_snapshots(self, before: dict, after: dict) -> dict:
        """SQL two-table comparison of two nodes snapshots.

        Returns both a qualified_name-keyed structure (for the micro/macro
        highlight wiring) and a JSON-friendly serialized change set.
        """
        added = {q: after[q] for q in after if q not in before}
        removed = {q: before[q] for q in before if q not in after}
        modified = {q: after[q] for q in before
                    if q in after and before[q]["hash"] != after[q]["hash"]}

        def info(qn: str, rec: dict) -> dict:
            return {
                "name": qn.split("::")[-1],
                "kind": rec["kind"],
                "parent": rec["parent"],
                "file": self.rel_path(rec["file"]),
            }

        return {
            "by_qn": {"added": added, "modified": modified,
                      "removed": removed},
            "serialized": {
                "added": [info(q, r) for q, r in sorted(added.items())],
                "modified": [info(q, r) for q, r in sorted(modified.items())],
                "removed": [info(q, r) for q, r in sorted(removed.items())],
            },
        }

    def _sync_micro_window(self) -> None:
        """Propagate the new change set to the detached micro window."""
        try:
            if self._micro_window is None or self._micro_bridge is None:
                return
            self._micro_bridge._change = self._change
            try:
                if bool(self._micro_window.native.Visible):
                    self._micro_window.evaluate_js(
                        "window.__codemap_reload &&"
                        " window.__codemap_reload(); 'ok'")
            except Exception:
                pass  # window hidden / page not ready is fine
        except Exception:
            pass

    def _change_for_module(self, key: str) -> dict:
        """Change-set info for one module's micro view.

        ``added_funcs`` are added Function nodes rendered as green ghost
        badges (functions are folded into classes/files, not drawn as nodes);
        ``removed`` are nodes that disappeared (red ghost badges). Class/file
        status flags are attached separately in get_micro().
        """
        ch = self._change or {}
        added = ch.get("added", {})
        removed = ch.get("removed", {})
        added_funcs = []
        for qn, info in sorted(added.items()):
            if info["kind"] != "Function" or self.module_of(info["file"]) != key:
                continue
            short = qn.split("::")[-1]
            if info["parent"]:
                short = info["parent"].split("::")[-1] + "." + short
            added_funcs.append({"name": short,
                                "file": self.rel_path(info["file"])})
        removed_ghosts = []
        for qn, info in sorted(removed.items()):
            if self.module_of(info["file"]) != key:
                continue
            short = qn.split("::")[-1]
            if info["kind"] == "Function" and info["parent"]:
                short = info["parent"].split("::")[-1] + "." + short
            removed_ghosts.append({"name": short, "kind": info["kind"],
                                   "file": self.rel_path(info["file"])})
        return {"added_funcs": added_funcs, "removed": removed_ghosts}

    def _change_counts(self) -> dict:
        """Per-module {added, modified, removed} counts for the macro view."""
        counts: dict[str, dict] = {}
        for kind in ("added", "modified", "removed"):
            for _qn, info in (self._change or {}).get(kind, {}).items():
                key = self.module_of(info["file"])
                slot = counts.setdefault(
                    key, {"added": 0, "modified": 0, "removed": 0})
                slot[kind] += 1
        return counts

    def close(self) -> dict:
        """Close the CodeMap window (called from the page's close button)."""
        if self._window is not None:
            self._window.destroy()
        return {"status": "ok"}

    def minimize(self) -> dict:
        """Minimize the window to the taskbar (called from the page's ─ button)."""
        if self._window is not None:
            self._window.minimize()
        return {"status": "ok"}

    def pop_micro(self, module_key: str) -> dict:
        """Pop the class diagram for ``module_key`` out into its own window.

        Called from the main page's ``⧉ 弹出`` button. The micro window is
        created hidden at startup; here we point it at the requested module,
        park it next to the main window, show it, and ask its page to
        re-render. Hiding it again is MicroBridge.close() (the ✕ button),
        so the same window can be popped out repeatedly.
        """
        if self._micro_window is None or self._micro_bridge is None:
            return {"error": "micro window not configured"}
        if module_key not in self._module_keys:
            return {"error": f"unknown module: {module_key}"}
        try:
            self._micro_bridge._module_key = module_key
            try:
                # Park the detached window just below/right of the main one
                # so it feels "dragged out" rather than centered somewhere.
                self._micro_window.move(self._window.x + 40, self._window.y + 40)
            except Exception:
                pass  # position is a nicety; show() is the important part
            self._micro_window.show()
            self._micro_window.evaluate_js(
                "window.__codemap_reload && window.__codemap_reload(); 'ok'")
            return {"status": "ok", "module": module_key}
        except Exception as exc:
            logger.error("pop_micro failed: %s", exc)
            return {"error": repr(exc)}

    def resize_begin(self) -> dict:
        """Snapshot the native window rect at the start of a border drag.

        Returns the physical-px ``{left, top, width, height}`` of the window.
        The page passes this snapshot back to every ``resize`` call so each
        call is an idempotent function of (baseline, delta): the new rect is
        derived from the snapshot, never from the live rect. Without the
        baseline, rapid pointermove events would race (pywebview serves each
        js_api call on its own thread) and compound the running deltas,
        making the window grow far faster than the mouse moves.
        """
        if self._window is None:
            return {"error": "no window"}
        try:
            import ctypes
            from ctypes import wintypes
        except ImportError:  # pragma: no cover - non-Windows
            return {"error": "ctypes unavailable"}

        hwnd = self._window.native.Handle.ToInt32()
        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return {"left": rect.left, "top": rect.top,
                "width": rect.right - rect.left,
                "height": rect.bottom - rect.top}

    def resize(self, edges: str, dx: float, dy: float,
               base: dict | None = None) -> dict:
        """Resize the native window while a border handle is dragged.

        ``edges`` names the dragged edge/corner ("n", "s", "e", "w", "ne",
        "nw", "se", "sw"); ``dx``/``dy`` are pointer deltas in physical
        screen pixels since the drag started; ``base`` is the
        ``{left, top, width, height}`` snapshot from ``resize_begin`` taken
        at drag start. The new rect is always computed from that snapshot,
        so overlapping bridge calls cannot compound. Implemented with plain
        Win32 calls so it is safe to run from the js_api worker thread.
        """
        if self._window is None:
            return {"error": "no window"}
        if not base or "width" not in base or "height" not in base:
            return {"error": "no baseline (call resize_begin first)"}
        try:
            import ctypes
            from ctypes import wintypes
        except ImportError:  # pragma: no cover - non-Windows
            return {"error": "ctypes unavailable"}

        hwnd = self._window.native.Handle.ToInt32()
        user32 = ctypes.windll.user32

        base_w = int(base["width"])
        base_h = int(base["height"])

        has = lambda side: side in edges  # noqa: E731
        # Deltas can be fractional on high-DPI screens (dpr 1.5 yields .5
        # values); round to the nearest physical px for the Win32 call.
        new_w = max(MIN_WIDTH, int(round(
            base_w + (dx if has("e") else 0) - (dx if has("w") else 0))))
        new_h = max(MIN_HEIGHT, int(round(
            base_h + (dy if has("s") else 0) - (dy if has("n") else 0))))

        # Edges opposite the dragged ones stay fixed at the baseline rect.
        x = int(round(base["left"] + base_w - new_w)) if has("w") \
            else int(base["left"])
        y = int(round(base["top"] + base_h - new_h)) if has("n") \
            else int(base["top"])

        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        user32.SetWindowPos(
            hwnd, None, x, y, new_w, new_h, SWP_NOZORDER | SWP_NOACTIVATE)
        return {"width": new_w, "height": new_h}

    # ---- Task 5: mermaid block-diagram data ----------------------------

    def _read_graph(self) -> tuple[list[dict], list[dict]]:
        """Read all nodes/edges (shared by the macro/micro aggregations)."""
        db_path = self.db_path
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            con.row_factory = sqlite3.Row
            nodes = [dict(r) for r in con.execute(
                f"SELECT {', '.join(NODE_COLS)} FROM nodes")]
            edges = [dict(r) for r in con.execute(
                f"SELECT {', '.join(EDGE_COLS)} FROM edges")]
        finally:
            con.close()
        return nodes, edges

    def _aggregate(self) -> dict:
        """Roll the raw graph up into the structures both views need.

        Mirrors make_arch_demo3.py: functions are folded into their parent
        class (or, when module-level, into their file), INHERITS short-name
        targets are resolved inside the same file, and REFERENCES/CALLS are
        counted per (source container, target class) pair.
        """
        nodes, edges = self._read_graph()

        classes = [(n["qualified_name"], n["file_path"])
                   for n in nodes if n["kind"] == "Class"]
        cls_set = {q for q, _ in classes}
        fp_of = {q: fp for q, fp in classes}
        short_to_qn: dict[tuple[str, str], list[str]] = defaultdict(list)
        for q, fp in classes:
            short_to_qn[(fp, q.split("::")[-1])].append(q)

        methods = [(n["qualified_name"], n["parent_name"])
                   for n in nodes if n["kind"] == "Function"]
        parent_of = {q: p for q, p in methods if p}
        methods_of: dict[str, list[str]] = defaultdict(list)
        for q, p in methods:
            if p:
                methods_of[p].append(q.split("::")[-1])

        def resolve_short(fp: str, short: str) -> str | None:
            got = short_to_qn.get((fp, short))
            return got[0] if got else None

        def container_of(qn: str) -> tuple[str, str, str]:
            """Roll an edge endpoint up to its owning class or file.

            Returns (kind, container_qualified_name, container_file_path).
            """
            if qn in cls_set:
                return "class", qn, fp_of[qn]
            parent = parent_of.get(qn)
            if parent and parent in cls_set:
                return "class", parent, fp_of[parent]
            fp = qn.rsplit("::", 1)[0] if "::" in qn else qn
            return "file", fp, fp

        classes_by_mod: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for q, fp in classes:
            classes_by_mod[self.module_of(fp)].append((q, fp))

        files_by_mod: dict[str, set[str]] = defaultdict(set)
        for n in nodes:
            if n["kind"] == "File":
                files_by_mod[self.module_of(n["file_path"])].add(n["file_path"])
        for q, fp in classes:
            files_by_mod[self.module_of(fp)].add(fp)

        # INHERITS resolved within the same module (short-name targets).
        inherits: list[tuple[str, str]] = []
        for e in edges:
            if e["kind"] != "INHERITS":
                continue
            src = e["source_qualified"]
            if src not in cls_set:
                continue
            tgt = resolve_short(fp_of[src], e["target_qualified"])
            if tgt and self.module_of(fp_of[tgt]) == self.module_of(fp_of[src]):
                inherits.append((src, tgt))

        # REFERENCES/CALLS/IMPORTS rolled up to containers; split into
        # intra-module refs (micro view) and cross-module pairs (macro view).
        refs_by_mod: dict[str, Counter] = defaultdict(Counter)
        cross_mod: Counter = Counter()   # (mod_src, mod_tgt) -> count
        dep_weight: Counter = Counter()  # file path -> incident dep count
        for e in edges:
            if e["kind"] not in DEP_KINDS:
                continue
            s_kind, s_cont, s_fp = container_of(e["source_qualified"])
            t_kind, t_cont, t_fp = container_of(e["target_qualified"])
            dep_weight[s_fp] += 1
            dep_weight[t_fp] += 1
            sm, tm = self.module_of(s_fp), self.module_of(t_fp)
            if sm != tm:
                cross_mod[(sm, tm)] += 1
            elif t_kind == "class":
                refs_by_mod[sm][(s_cont, t_cont)] += 1

        return {
            "classes_by_mod": classes_by_mod,
            "files_by_mod": files_by_mod,
            "methods_of": methods_of,
            "inherits": inherits,
            "refs_by_mod": refs_by_mod,
            "cross_mod": cross_mod,
            "dep_weight": dep_weight,
            "total_edges": len(edges),
        }

    def get_macro(self) -> dict:
        """Business-module summary for the macro (module) view."""
        try:
            agg = self._aggregate()
        except sqlite3.Error as exc:
            logger.error("get_macro failed: %s", exc)
            return {"error": str(exc)}

        change_counts = self._change_counts()
        modules = []
        for key, name, icon in self.modules:
            cls_list = agg["classes_by_mod"].get(key, [])
            files = agg["files_by_mod"].get(key, set())
            # Key files: most dependency traffic first, then most classes.
            ranked = sorted(
                files,
                key=lambda fp: (-agg["dep_weight"].get(fp, 0),
                                -sum(1 for _, c_fp in cls_list if c_fp == fp),
                                fp),
            )
            cap = 6
            modules.append({
                "key": key, "name": name, "icon": icon,
                "class_count": len(cls_list),
                "file_count": len(files),
                "files": [self.rel_path(fp) for fp in ranked[:cap]],
                "more_files": max(0, len(ranked) - cap),
                "change": change_counts.get(
                    key, {"added": 0, "modified": 0, "removed": 0}),
            })

        edges = [{"from": s, "to": t, "count": n}
                 for (s, t), n in sorted(agg["cross_mod"].items(),
                                         key=lambda x: -x[1])]
        return {
            "modules": modules,
            "edges": edges,
            "stats": {
                "classes": sum(len(v) for v in agg["classes_by_mod"].values()),
                "files": sum(len(v) for v in agg["files_by_mod"].values()),
                "total_edges": agg["total_edges"],
                "cross_edges": sum(agg["cross_mod"].values()),
            },
        }

    def get_micro(self, module_key: str) -> dict:
        """Class diagram for one business module (micro view)."""
        try:
            agg = self._aggregate()
        except sqlite3.Error as exc:
            logger.error("get_micro failed: %s", exc)
            return {"error": str(exc)}

        if module_key not in self._module_keys:
            return {"error": f"unknown module: {module_key}"}

        cls_list = sorted(agg["classes_by_mod"].get(module_key, []))
        cls_qns = {qn for qn, _ in cls_list}
        base_set = {t for _, t in agg["inherits"]}
        added_qns = set((self._change or {}).get("added", {}))
        modified_qns = set((self._change or {}).get("modified", {}))
        classes = [
            {
                "name": qn.split("::")[-1],
                "file": self.rel_path(fp),
                "is_base": qn in base_set,
                "methods": sorted(agg["methods_of"].get(qn, [])),
                "status": ("added" if qn in added_qns
                           else "modified" if qn in modified_qns else None),
            }
            for qn, fp in cls_list
        ]

        files = {fp for _, fp in cls_list}
        refs = []
        for (src, tgt), n in sorted(
                agg["refs_by_mod"].get(module_key, {}).items(),
                key=lambda x: -x[1]):
            if tgt not in cls_qns:
                continue
            if src in cls_qns:
                refs.append({"from_class": src.split("::")[-1],
                             "to_class": tgt.split("::")[-1], "count": n})
            else:
                files.add(src)
                refs.append({"from_file": self.rel_path(src),
                             "to_class": tgt.split("::")[-1], "count": n})

        inherits = [{"from": s.split("::")[-1], "to": t.split("::")[-1]}
                    for s, t in agg["inherits"] if s in cls_qns]

        return {
            "module": {"key": module_key,
                       "name": self._mod_name[module_key],
                       "icon": self._mod_icon[module_key]},
            "classes": classes,
            "files": [
                {"rel": self.rel_path(fp),
                 "status": ("added" if fp in added_qns
                            else "modified" if fp in modified_qns else None)}
                for fp in sorted(files)
            ],
            "inherits": inherits,
            "refs": refs,
            "change": self._change_for_module(module_key),
            "stats": {"classes": len(classes), "files": len(files),
                      "edges": len(inherits) + len(refs)},
        }


class MicroBridge(Bridge):
    """js_api for the detached micro (class-diagram) window.

    Shares every data method with the main bridge; the module is stored per
    instance so the detached window always renders whatever module the main
    window last asked it to pop out. ``close()`` hides instead of destroying,
    so the same native window can be re-popped from the main window.
    """

    def __init__(self, module_key: str = "AUTH") -> None:
        super().__init__()
        self._module_key = module_key

    def get_micro(self, key: str | None = None) -> dict:
        return super().get_micro(key or self._module_key)

    def current(self) -> dict:
        return {"module": self._module_key,
                "name": self._mod_name.get(self._module_key, "")}

    def close(self) -> dict:
        if self._window is not None:
            self._window.hide()
        return {"status": "ok"}
