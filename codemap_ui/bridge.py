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

# ---- business-module split (same rules as make_arch_demo3.py) ----------
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


def module_of(path: str) -> str:
    """Map a file path to a business module (mirrors make_arch_demo3.module_of)."""
    p = (path or "").replace("\\", "/")
    for key, mod in (("/users", "AUTH"), ("/news", "NEWS"),
                     ("/favorite", "FAV"), ("/history", "HIST")):
        if key in p:
            return mod
    return "SUPPORT"


def rel_path(path: str) -> str:
    """Short display path relative to the repo root (e.g. ``models/users.py``)."""
    p = (path or "").replace("\\", "/")
    if REPO_ROOT in p:
        return p.split(REPO_ROOT, 1)[1]
    return p.rsplit("/", 1)[-1]


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

    def get_graph(self) -> dict:
        """Read nodes/edges from graph.db and return them as JSON data."""
        db_path = get_db_path()
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
                     "--repo", REPO_ROOT, "-q"],
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
        """graph.db mtime + path, for the JS polling loop (every 2 s)."""
        db_path = get_db_path()
        try:
            return {"mtime": os.path.getmtime(db_path),
                    "db": db_path, "exists": True}
        except OSError:
            return {"mtime": None, "db": db_path, "exists": False}

    def _db_mtime(self) -> float | None:
        try:
            return os.path.getmtime(get_db_path())
        except OSError:
            return None

    def _snapshot_nodes(self) -> dict:
        """Read the nodes table as {qualified_name: info} (read-only)."""
        db_path = get_db_path()
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT qualified_name, kind, parent_name, file_path,"
                " file_hash FROM nodes").fetchall()
        finally:
            con.close()
        return {r[0]: {"kind": r[1], "parent": r[2], "file": r[3],
                       "hash": r[4]} for r in rows}

    @staticmethod
    def _diff_snapshots(before: dict, after: dict) -> dict:
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
                "file": rel_path(rec["file"]),
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
            if info["kind"] != "Function" or module_of(info["file"]) != key:
                continue
            short = qn.split("::")[-1]
            if info["parent"]:
                short = info["parent"].split("::")[-1] + "." + short
            added_funcs.append({"name": short, "file": rel_path(info["file"])})
        removed_ghosts = []
        for qn, info in sorted(removed.items()):
            if module_of(info["file"]) != key:
                continue
            short = qn.split("::")[-1]
            if info["kind"] == "Function" and info["parent"]:
                short = info["parent"].split("::")[-1] + "." + short
            removed_ghosts.append({"name": short, "kind": info["kind"],
                                   "file": rel_path(info["file"])})
        return {"added_funcs": added_funcs, "removed": removed_ghosts}

    def _change_counts(self) -> dict:
        """Per-module {added, modified, removed} counts for the macro view."""
        counts: dict[str, dict] = {}
        for kind in ("added", "modified", "removed"):
            for _qn, info in (self._change or {}).get(kind, {}).items():
                key = module_of(info["file"])
                slot = counts.setdefault(
                    key, {"added": 0, "modified": 0, "removed": 0})
                slot[kind] += 1
        return counts

    def close(self) -> dict:
        """Close the CodeMap window (called from the page's close button)."""
        if self._window is not None:
            self._window.destroy()
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
        if module_key not in MODULE_KEYS:
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
        db_path = get_db_path()
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
            classes_by_mod[module_of(fp)].append((q, fp))

        files_by_mod: dict[str, set[str]] = defaultdict(set)
        for n in nodes:
            if n["kind"] == "File":
                files_by_mod[module_of(n["file_path"])].add(n["file_path"])
        for q, fp in classes:
            files_by_mod[module_of(fp)].add(fp)

        # INHERITS resolved within the same module (short-name targets).
        inherits: list[tuple[str, str]] = []
        for e in edges:
            if e["kind"] != "INHERITS":
                continue
            src = e["source_qualified"]
            if src not in cls_set:
                continue
            tgt = resolve_short(fp_of[src], e["target_qualified"])
            if tgt and module_of(fp_of[tgt]) == module_of(fp_of[src]):
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
            sm, tm = module_of(s_fp), module_of(t_fp)
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
        for key, name, icon in MODULES:
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
                "files": [rel_path(fp) for fp in ranked[:cap]],
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

        if module_key not in MODULE_KEYS:
            return {"error": f"unknown module: {module_key}"}

        cls_list = sorted(agg["classes_by_mod"].get(module_key, []))
        cls_qns = {qn for qn, _ in cls_list}
        base_set = {t for _, t in agg["inherits"]}
        added_qns = set((self._change or {}).get("added", {}))
        modified_qns = set((self._change or {}).get("modified", {}))
        classes = [
            {
                "name": qn.split("::")[-1],
                "file": rel_path(fp),
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
                refs.append({"from_file": rel_path(src),
                             "to_class": tgt.split("::")[-1], "count": n})

        inherits = [{"from": s.split("::")[-1], "to": t.split("::")[-1]}
                    for s, t in agg["inherits"] if s in cls_qns]

        return {
            "module": {"key": module_key, "name": MOD_NAME[module_key],
                       "icon": MOD_ICON[module_key]},
            "classes": classes,
            "files": [
                {"rel": rel_path(fp),
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
                "name": MOD_NAME.get(self._module_key, "")}

    def close(self) -> dict:
        if self._window is not None:
            self._window.hide()
        return {"status": "ok"}
