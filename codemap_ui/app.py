"""CodeMap floating window entry point.

Run:  python -m codemap_ui.app
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import webview

from .bridge import CODEMAP_ROOT, REPO_ROOT, Bridge, MicroBridge
from .config import (
    APP_TITLE,
    EASY_DRAG,
    FRAMELESS,
    ON_TOP,
    RESIZABLE,
    WINDOW_HEIGHT,
    WINDOW_WIDTH,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_URL = str(STATIC_DIR / "index.html")
MICRO_URL = str(STATIC_DIR / "micro.html")

MICRO_TITLE = "CodeMap · 微观"
MICRO_WIDTH = 680
MICRO_HEIGHT = 560


def _enable_borderless_resize(window: webview.Window) -> None:
    """Restore edge-resizing on the frameless window (Windows only).

    pywebview's winforms backend sets ``FormBorderStyle.None`` for frameless
    windows, which also drops the native resize border. Re-adding the
    ``WS_THICKFRAME`` style re-enables drag-to-resize (and Aero snap) without
    bringing back a caption bar. No-op on other platforms.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        GWL_STYLE = -16
        WS_THICKFRAME = 0x00040000
        SWP_FRAMECHANGED = 0x0020
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010

        user32 = ctypes.windll.user32
        hwnd = window.native.Handle.ToInt32()
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        user32.SetWindowLongW(hwnd, GWL_STYLE, style | WS_THICKFRAME)
        user32.SetWindowPos(
            hwnd, None, 0, 0, 0, 0,
            SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE
            | SWP_NOZORDER | SWP_NOACTIVATE,
        )
    except Exception as exc:  # pragma: no cover - platform dependent
        logger.warning("Could not enable borderless resize: %s", exc)


def _window_chrome_flags(window: webview.Window) -> dict:
    """Read back native window styles for verification (Windows only)."""
    flags: dict = {}
    if sys.platform != "win32":
        return flags
    try:
        import ctypes

        GWL_STYLE = -16
        GWL_EXSTYLE = -20
        WS_CAPTION = 0x00C00000
        WS_THICKFRAME = 0x00040000
        WS_EX_TOPMOST = 0x00000008

        user32 = ctypes.windll.user32
        hwnd = window.native.Handle.ToInt32()
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        flags = {
            "frameless": not bool(style & WS_CAPTION),
            "resizable_edge": bool(style & WS_THICKFRAME),
            "on_top": bool(exstyle & WS_EX_TOPMOST),
            "style": hex(style),
            "exstyle": hex(exstyle),
        }
    except Exception as exc:  # pragma: no cover - platform dependent
        flags["error"] = repr(exc)
    return flags


def create_window(bridge: Bridge | None = None) -> webview.Window:
    """Create the CodeMap window with the configured chrome and js_api."""
    bridge = bridge or Bridge()
    window = webview.create_window(
        APP_TITLE,
        url=INDEX_URL,
        js_api=bridge,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        frameless=FRAMELESS,
        on_top=ON_TOP,
        easy_drag=EASY_DRAG,
        resizable=RESIZABLE,
    )
    bridge._window = window  # allow Bridge.close() to destroy the window
    return window


def create_micro_window(bridge: MicroBridge) -> webview.Window:
    """Create the detached micro (class-diagram) window, hidden for now.

    The window is created up front (pywebview needs all windows before
    ``webview.start()``) and simply hidden until the user clicks ``⧉ 弹出``,
    at which point Bridge.pop_micro() shows it and re-renders its page.
    """
    window = webview.create_window(
        MICRO_TITLE,
        url=MICRO_URL,
        js_api=bridge,
        width=MICRO_WIDTH,
        height=MICRO_HEIGHT,
        frameless=FRAMELESS,
        on_top=ON_TOP,
        easy_drag=EASY_DRAG,
        resizable=RESIZABLE,
        hidden=True,
    )
    bridge._window = window
    return window


def _safe_destroy(win: webview.Window) -> None:
    try:
        win.destroy()
    except Exception as exc:  # pragma: no cover - already-closed window
        logger.warning("Could not destroy window: %s", exc)


def _smoke_test(bridge: Bridge, window: webview.Window,
                micro_window: webview.Window,
                micro_bridge: MicroBridge) -> None:
    """Open the windows, verify the views/bridge, then close."""
    result: dict = {}

    def on_loaded(*_args) -> None:  # noqa: ANN002 - event args vary by pywebview
        time.sleep(4)  # allow the async get_macro() round-trip + render to finish
        try:
            result["title"] = window.evaluate_js("document.title")
            result["status"] = window.evaluate_js(
                "document.getElementById('status').textContent")
            result["counts"] = window.evaluate_js(
                "JSON.stringify({nodes: window.__codemap_nodes, "
                "edges: window.__codemap_edges})")
            # ---- macro view: 5 module subgraphs, orthogonal step edges ----
            result["macro"] = window.evaluate_js(
                "JSON.stringify({"
                "svg: !!document.querySelector('#graph svg'),"
                "clusters: document.querySelectorAll('#graph g.cluster').length,"
                "nodes: document.querySelectorAll('#graph g.node').length,"
                "edges: document.querySelectorAll('#graph path.flowchart-link').length,"
                "edgeLabels: Array.from(document.querySelectorAll('#graph .edgeLabel')).map("
                "function(e){return e.textContent;}).filter(function(t){return t.trim().length>0;}).join(','),"
                "orthogonal: Array.from(document.querySelectorAll('#graph path')).every("
                "function(p){var d=p.getAttribute('d')||'';return !/[CSQAT]/i.test(d);}),"
                "bodyBg: getComputedStyle(document.body).backgroundColor,"
                "clusterFill: (function(){var r=document.querySelector('#graph g.cluster rect');"
                "return r?getComputedStyle(r).fill:null;})(),"
                "clusterStroke: (function(){var r=document.querySelector('#graph g.cluster rect');"
                "return r?getComputedStyle(r).stroke:null;})(),"
                "nodeFill: (function(){var r=document.querySelector('#graph g.node rect');"
                "return r?getComputedStyle(r).fill:null;})(),"
                "edgeStroke: (function(){var p=document.querySelector('#graph path.flowchart-link');"
                "return p?getComputedStyle(p).stroke:null;})(),"
                "fit: (function(){var s=document.querySelector('#graph svg'),g=document.getElementById('graph');"
                "if(!s)return null;var sr=s.getBoundingClientRect(),gr=g.getBoundingClientRect();"
                "return JSON.stringify({sw:Math.round(sr.width),sh:Math.round(sr.height),"
                "gw:Math.round(gr.width),gh:Math.round(gr.height)});})(),"
                "view: window.__codemap_view,"
                "status: document.getElementById('status').textContent})")
            macro1 = json.loads(result["macro"])
            assert macro1["orthogonal"], (
                "macro edges must be strictly orthogonal: %s"
                % result["macro"])
            # ---- zoom: Ctrl+wheel zooms, reset returns to 100% fit ---------
            result["zoom0"] = window.evaluate_js(
                "JSON.stringify({z: window.__codemap_zoom,"
                "w: (function(){var m=document.querySelector('#graph .mermaid');"
                "return m?m.style.width:null;})()})")
            # plain wheel zooms at the cursor (no Ctrl needed)
            window.evaluate_js(
                "(function(){var g=document.getElementById('graph');"
                "var r=g.getBoundingClientRect();"
                "function fire(dy){g.dispatchEvent(new WheelEvent('wheel',"
                "{deltaY:dy,bubbles:true,cancelable:true,"
                "clientX:r.left+r.width/2,clientY:r.top+r.height/2}));}"
                "fire(-120);fire(-120);return 'ok';})()")
            time.sleep(1)
            result["zoom_in"] = window.evaluate_js(
                "JSON.stringify({z: window.__codemap_zoom,"
                "label: document.getElementById('zoom-reset').textContent,"
                "scrollable: (function(){var g=document.getElementById('graph');"
                "return g.scrollWidth>g.clientWidth||g.scrollHeight>g.clientHeight;})()})")
            # left-drag pans the zoomed diagram (map-style)
            result["pan0"] = window.evaluate_js(
                "JSON.stringify({sl: document.getElementById('graph').scrollLeft,"
                "st: document.getElementById('graph').scrollTop})")
            window.evaluate_js(
                "(function(){var g=document.getElementById('graph');"
                "function ev(t,x,y){return new PointerEvent(t,{button:0,"
                "bubbles:true,cancelable:true,pointerId:7,clientX:x,clientY:y});}"
                "g.dispatchEvent(ev('pointerdown',400,300));"
                "g.dispatchEvent(ev('pointermove',250,200));"
                "g.dispatchEvent(ev('pointerup',250,200));return 'ok';})()")
            time.sleep(1)
            result["pan1"] = window.evaluate_js(
                "JSON.stringify({sl: document.getElementById('graph').scrollLeft,"
                "st: document.getElementById('graph').scrollTop,"
                "z: window.__codemap_zoom})")
            window.evaluate_js("document.getElementById('zoom-reset').click()")
            time.sleep(1)
            result["zoom_reset"] = window.evaluate_js(
                "JSON.stringify({z: window.__codemap_zoom,"
                "label: document.getElementById('zoom-reset').textContent})")
            # ---- double-click the AUTH module -> micro (class) view -------
            window.evaluate_js(
                "(function(){var c=null;document.querySelectorAll('#graph g.cluster').forEach("
                "function(g){if((g.getAttribute('id')||'').indexOf('GAUTH')!==-1)c=g;});"
                "if(!c){return 'no-cluster';}"
                "c.dispatchEvent(new MouseEvent('dblclick',{bubbles:true,cancelable:true}));"
                "return 'ok';})()")
            time.sleep(2)
            result["micro"] = window.evaluate_js(
                "JSON.stringify({"
                "view: window.__codemap_view,"
                "z: window.__codemap_zoom,"
                "clusters: document.querySelectorAll('#graph g.cluster').length,"
                "nodes: document.querySelectorAll('#graph g.node').length,"
                "edges: document.querySelectorAll('#graph path.flowchart-link').length,"
                "solidEdges: document.querySelectorAll('#graph path.edge-pattern-solid').length,"
                "dashedEdges: document.querySelectorAll("
                "'#graph path.edge-pattern-dotted,#graph path.edge-pattern-dashed').length,"
                "orthogonal: Array.from(document.querySelectorAll('#graph path')).every("
                "function(p){var d=p.getAttribute('d')||'';return !/[CSQAT]/i.test(d);}),"
                "nodeFills: JSON.stringify(Array.from(new Set(Array.from("
                "document.querySelectorAll('#graph g.node rect')).filter("
                "function(r){return r.getBoundingClientRect().width>0 "
                "&& r.getBoundingClientRect().height>0;}).map("
                "function(r){return getComputedStyle(r).fill;})))),"
                "edgeLabels: Array.from(document.querySelectorAll('#graph .edgeLabel')).map("
                "function(e){return e.textContent;}).filter(function(t){return t.trim().length>0;}).join(','),"
                "microActive: document.getElementById('view-micro').classList.contains('active'),"
                "status: document.getElementById('status').textContent})")
            micro1 = json.loads(result["micro"])
            assert micro1["orthogonal"], (
                "micro edges must be strictly orthogonal: %s" % result["micro"])
            # ---- explicit view switch: 「宏观」button -> macro view --------
            window.evaluate_js("document.getElementById('view-macro').click()")
            time.sleep(2)
            result["macro2"] = window.evaluate_js(
                "JSON.stringify({view: window.__codemap_view,"
                "clusters: document.querySelectorAll('#graph g.cluster').length,"
                "macroActive: document.getElementById('view-macro').classList.contains('active'),"
                "status: document.getElementById('status').textContent})")
            # ---- 「微观」button + module dropdown --------------------------
            window.evaluate_js("document.getElementById('view-micro').click()")
            time.sleep(1.5)
            result["switch_micro"] = window.evaluate_js(
                "JSON.stringify({view: window.__codemap_view,"
                "microActive: document.getElementById('view-micro').classList.contains('active'),"
                "status: document.getElementById('status').textContent})")
            # ---- searchable module picker: fuzzy search + click select ---
            # typing "his" must fuzzy-match only the HIST module
            window.evaluate_js(
                "(function(){var i=document.getElementById('module-search');"
                "i.value='his';i.dispatchEvent(new Event('input',{bubbles:true}));return 'ok';})()")
            time.sleep(0.8)
            result["fuzzy_his"] = window.evaluate_js(
                "JSON.stringify({open: !document.getElementById('module-list').hidden,"
                "keys: Array.from(document.querySelectorAll('#module-list .item .hint'))"
                ".map(function(e){return e.textContent;}).join(','),"
                "input: document.getElementById('module-search').value})")
            window.evaluate_js(
                "(function(){var it=document.querySelector('#module-list .item');"
                "it.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));return 'ok';})()")
            time.sleep(1.5)
            result["pick_hist"] = window.evaluate_js(
                "JSON.stringify({view: window.__codemap_view,"
                "status: document.getElementById('status').textContent,"
                "input: document.getElementById('module-search').value})")
            # Chinese fuzzy search: "新闻" must match the news module
            window.evaluate_js(
                "(function(){var i=document.getElementById('module-search');"
                "i.value='新闻';i.dispatchEvent(new Event('input',{bubbles:true}));return 'ok';})()")
            time.sleep(0.8)
            result["fuzzy_news"] = window.evaluate_js(
                "JSON.stringify({keys: Array.from(document.querySelectorAll('#module-list .item .hint'))"
                ".map(function(e){return e.textContent;}).join(',')})")
            window.evaluate_js(
                "(function(){var it=document.querySelector('#module-list .item');"
                "it.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));return 'ok';})()")
            time.sleep(1.5)
            result["switch_news"] = window.evaluate_js(
                "JSON.stringify({view: window.__codemap_view,"
                "status: document.getElementById('status').textContent,"
                "selected: document.getElementById('module-search').value})")
            # ---- user-path picker: focus opens dropdown + fuzzy replace ---
            # The box is pre-filled ("📰 新闻模块"); a real user clicks it and
            # types. Focus must select-all (so typing REPLACES the display
            # value instead of appending to it) and open the dropdown showing
            # every module.
            result["focus_open"] = window.evaluate_js(
                "(function(){var i=document.getElementById('module-search');"
                "i.focus();i.dispatchEvent(new Event('focus'));"
                "return JSON.stringify({"
                "open: !document.getElementById('module-list').hidden,"
                "keys: Array.from(document.querySelectorAll('#module-list .item .hint'))"
                ".map(function(e){return e.textContent;}).join(','),"
                "value: i.value,"
                "sel: i.selectionStart===0 && i.selectionEnd===i.value.length"
                "&& i.value.length>0});})()")
            # select-all makes the first keystroke replace the display value
            # -> the box now holds the real fuzzy query "his".
            window.evaluate_js(
                "(function(){var i=document.getElementById('module-search');"
                "i.value='his';i.dispatchEvent(new Event('input',{bubbles:true}));"
                "return 'ok';})()")
            time.sleep(0.8)
            result["fuzzy_user_his"] = window.evaluate_js(
                "JSON.stringify({open: !document.getElementById('module-list').hidden,"
                "keys: Array.from(document.querySelectorAll('#module-list .item .hint'))"
                ".map(function(e){return e.textContent;}).join(','),"
                "value: document.getElementById('module-search').value,"
                "empty: !!document.querySelector('#module-list .empty')})")
            # Enter on the highlighted item selects HIST (micro view).
            window.evaluate_js(
                "(function(){var i=document.getElementById('module-search');"
                "i.dispatchEvent(new KeyboardEvent('keydown',"
                "{key:'Enter',bubbles:true,cancelable:true}));return 'ok';})()")
            time.sleep(1.5)
            result["user_enter_hist"] = window.evaluate_js(
                "JSON.stringify({view: window.__codemap_view,"
                "selected: document.getElementById('module-search').value,"
                "open: !document.getElementById('module-list').hidden})")
            # Re-focus: dropdown opens again with all 5 modules (dropdown
            # mode), then a Chinese query filters to the news module.
            result["focus_again"] = window.evaluate_js(
                "(function(){var i=document.getElementById('module-search');"
                "i.focus();i.dispatchEvent(new Event('focus'));"
                "return JSON.stringify({"
                "open: !document.getElementById('module-list').hidden,"
                "keys: Array.from(document.querySelectorAll('#module-list .item .hint'))"
                ".map(function(e){return e.textContent;}).join(',')});})()")
            window.evaluate_js(
                "(function(){var i=document.getElementById('module-search');"
                "i.value='新闻';i.dispatchEvent(new Event('input',{bubbles:true}));"
                "return 'ok';})()")
            time.sleep(0.8)
            result["fuzzy_user_news"] = window.evaluate_js(
                "JSON.stringify({keys: Array.from(document.querySelectorAll('#module-list .item .hint'))"
                ".map(function(e){return e.textContent;}).join(',')})")
            window.evaluate_js(
                "(function(){var i=document.getElementById('module-search');"
                "i.dispatchEvent(new KeyboardEvent('keydown',"
                "{key:'Enter',bubbles:true,cancelable:true}));return 'ok';})()")
            time.sleep(1.5)
            result["user_enter_news"] = window.evaluate_js(
                "JSON.stringify({view: window.__codemap_view,"
                "selected: document.getElementById('module-search').value})")
            # No match -> dropdown shows the empty hint, not a crash.
            window.evaluate_js(
                "(function(){var i=document.getElementById('module-search');"
                "i.focus();i.dispatchEvent(new Event('focus'));"
                "i.value='zzz';i.dispatchEvent(new Event('input',{bubbles:true}));"
                "return 'ok';})()")
            time.sleep(0.8)
            result["fuzzy_nomatch"] = window.evaluate_js(
                "JSON.stringify({open: !document.getElementById('module-list').hidden,"
                "empty: !!document.querySelector('#module-list .empty'),"
                "text: document.querySelector('#module-list .empty')"
                "? document.querySelector('#module-list .empty').textContent : ''})")
            # ---- user-path picker: the real-user simulation must pass ----
            fr = json.loads(result["focus_open"])
            assert fr["open"], "focus must open the dropdown"
            assert fr["keys"] == "AUTH,NEWS,FAV,HIST,SUPPORT", (
                "focus dropdown must show all modules, got %s" % fr["keys"])
            assert fr["sel"], "focus must select-all the display value"
            assert fr["value"] == "📰 新闻模块", fr["value"]
            fh = json.loads(result["fuzzy_user_his"])
            assert fh["open"] and fh["keys"] == "HIST" and not fh["empty"], fh
            assert fh["value"] == "his", (
                "typing must REPLACE the display value, got %r" % fh["value"])
            eh = json.loads(result["user_enter_hist"])
            assert eh["view"] == "micro", eh
            assert eh["selected"] == "🕘 历史模块", eh
            assert not eh["open"], "list must close after Enter selection"
            fa = json.loads(result["focus_again"])
            assert fa["open"] and fa["keys"] == "AUTH,NEWS,FAV,HIST,SUPPORT", fa
            fn = json.loads(result["fuzzy_user_news"])
            assert fn["keys"] == "NEWS", fn
            en = json.loads(result["user_enter_news"])
            assert en["view"] == "micro" and en["selected"] == "📰 新闻模块", en
            nm = json.loads(result["fuzzy_nomatch"])
            assert nm["open"] and nm["empty"] and nm["text"] == "没有匹配的板块", nm
            # ---- easy_drag must NOT arm from the picker -------------------
            # pywebview arms window-dragging on any window-level mousedown;
            # the picker container stops propagation, so a press on the
            # search box must never reach the window (no "mouse glued" drag),
            # while a press on the graph still does (window stays draggable).
            result["drag_probe"] = window.evaluate_js(
                "(function(){"
                "window.__drag_probe=0;"
                "window.addEventListener('mousedown',function(){window.__drag_probe++;});"
                "var i=document.getElementById('module-search');"
                "i.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));"
                "var pickerArmed=window.__drag_probe;"
                "document.getElementById('graph')"
                ".dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));"
                "var graphArmed=window.__drag_probe;"
                "window.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));"
                "return JSON.stringify({pickerArmed:pickerArmed,graphArmed:graphArmed});})()")
            dp = json.loads(result["drag_probe"])
            assert dp["pickerArmed"] == 0, (
                "mousedown on the search box must not reach the window: %s" % dp)
            assert dp["graphArmed"] == 1, (
                "mousedown on the graph must still reach the window: %s" % dp)
            # ---- bottom legend must not overlap the status text -----------
            result["legend_overlap"] = window.evaluate_js(
                "(function(){var l=document.getElementById('legend')"
                ".getBoundingClientRect();"
                "var s=document.getElementById('status')"
                ".getBoundingClientRect();"
                "var overlap=!(l.right<=s.left||s.right<=l.left"
                "||l.bottom<=s.top||s.bottom<=l.top);"
                "return JSON.stringify({overlap:overlap,"
                "legend:{l:Math.round(l.left),r:Math.round(l.right),"
                "t:Math.round(l.top),b:Math.round(l.bottom)},"
                "status:{l:Math.round(s.left),r:Math.round(s.right),"
                "t:Math.round(s.top),b:Math.round(s.bottom)}});})()")
            lo = json.loads(result["legend_overlap"])
            assert not lo["overlap"], "legend overlaps status: %s" % lo
            # ---- pop the micro view out into its own window ---------------
            window.evaluate_js("void window.pywebview.api.pop_micro('FAV')")
            time.sleep(2.5)
            result["pop"] = micro_window.evaluate_js(
                "JSON.stringify({title: document.getElementById('micro-title').textContent,"
                "svg: !!document.querySelector('#graph svg'),"
                "nodes: document.querySelectorAll('#graph g.node').length,"
                "edges: document.querySelectorAll('#graph path.flowchart-link').length})")
            result["pop_visible"] = bool(micro_window.native.Visible)
            _enable_borderless_resize(micro_window)
            result["micro_chrome"] = _window_chrome_flags(micro_window)
            # hide it again, then re-pop a different module
            micro_bridge.close()
            time.sleep(0.6)
            result["pop_hidden"] = not bool(micro_window.native.Visible)
            window.evaluate_js("void window.pywebview.api.pop_micro('HIST')")
            time.sleep(2)
            result["pop2"] = micro_window.evaluate_js(
                "JSON.stringify({title: document.getElementById('micro-title').textContent,"
                "nodes: document.querySelectorAll('#graph g.node').length})")

            size_before = window.evaluate_js("JSON.stringify({w: window.innerWidth, h: window.innerHeight})")
            dpr = window.evaluate_js("JSON.stringify({dpr: window.devicePixelRatio})")

            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            hwnd = window.native.Handle.ToInt32()

            def native_rect():
                r = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                return (r.right - r.left, r.bottom - r.top)

            rect_before = native_rect()
            # Drag flow: snapshot the baseline once (resize_begin), then
            # resize from it. Fractional deltas simulate high-DPI (dpr 1.5)
            # pointer values; round() must yield an exact +120/+80 move.
            base = bridge.resize_begin()
            assert base.get("width") and base.get("height"), base
            assert (base["width"], base["height"]) == rect_before, (
                "baseline must match the live rect: %s vs %s" % (base, rect_before))
            window.evaluate_js(
                "void window.pywebview.api.resize('se', 120.5, 80.5, %s)"
                % json.dumps(base))
            time.sleep(1)  # let the resize round-trip land
            rect_after = native_rect()
            # Exactly ONE delta applied, not delta-on-top-of-delta.
            assert (rect_after[0] - rect_before[0],
                    rect_after[1] - rect_before[1]) == (120, 80), (
                "single-step resize wrong: %s -> %s" % (rect_before, rect_after))
            # Same baseline, bigger delta: the final size must equal
            # base + round(200.5/120.5) == +200/+120, NOT +320/+200 — the
            # compounding bug that made the resize feel hypersensitive.
            window.evaluate_js(
                "void window.pywebview.api.resize('se', 200.5, 120.5, %s)"
                % json.dumps(base))
            time.sleep(1)
            rect_final = native_rect()
            assert (rect_final[0] - rect_before[0],
                    rect_final[1] - rect_before[1]) == (200, 120), (
                "compounding regression: %s -> %s" % (rect_before, rect_final))
            size_after = window.evaluate_js("JSON.stringify({w: window.innerWidth, h: window.innerHeight})")
            result["size_before"] = size_before
            result["size_after"] = size_after
            result["dpr"] = dpr
            result["rect_before"] = rect_before
            result["rect_after"] = rect_after
            result["rect_final"] = rect_final

            # ---- incremental refresh: real edit -> CRG update -> SQL diff -
            # Edit models/users.py for real (modify User.__repr__, drop
            # UserToken.__repr__, append a module-level function), then click
            # 「刷新」. The bridge runs `code-review-graph update --repo ...`
            # and diffs the nodes table before/after (file_hash deltas +
            # qualified_name add/remove). Expected change set:
            #   added    codemap_smoke_added (Function)
            #   removed  UserToken.__repr__ (Function)
            #   modified File/Base/User/User.__repr__/UserToken (hash change)
            users_path = Path(REPO_ROOT) / "models" / "users.py"
            orig_bytes = users_path.read_bytes()
            newline = "\r\n" if b"\r\n" in orig_bytes else "\n"
            # Normalise to LF for editing; the file is re-encoded with its
            # original newline style and restored byte-for-byte, so git (with
            # core.autocrlf) never sees the smoke test in the working tree.
            orig_users = orig_bytes.decode("utf-8").replace("\r\n", "\n")
            marker = "nickname='{self.nickname}')>\""
            tok_repr = ('    def __repr__(self):\n'
                        '        return f"<UserToken(id={self.id}, '
                        "user_id={self.user_id}, token='{self.token}')>\"")
            assert marker in orig_users and tok_repr in orig_users
            edited = orig_users.replace(
                marker, "nickname='{self.nickname}', smoke=True)>\"")
            edited = edited.replace(
                tok_repr, "    # __repr__ removed by CodeMap smoke test")
            edited += ('\n\ndef codemap_smoke_added() -> str:\n'
                       '    """SMOKE-TEST: added function."""\n'
                       '    return "codemap"\n')
            restored_ok = False
            try:
                users_path.write_bytes(
                    edited.replace("\n", newline).encode("utf-8"))

                snap_before = bridge._snapshot_nodes()
                base_users_hash = next(
                    v["hash"] for q, v in snap_before.items()
                    if q.rstrip("/").endswith("models/users.py"))
                window.evaluate_js(
                    "document.getElementById('refresh').click(); 'ok'")
                time.sleep(4)  # CRG update (~0.8 s) + re-pull + render
                snap_after = bridge._snapshot_nodes()
                diff = bridge._diff_snapshots(snap_before, snap_after)
                q_added = set(diff["by_qn"]["added"])
                q_removed = set(diff["by_qn"]["removed"])
                q_modified = set(diff["by_qn"]["modified"])
                assert any(q.endswith("::codemap_smoke_added")
                           for q in q_added)
                assert any(q.endswith("::UserToken.__repr__")
                           for q in q_removed)
                assert any(q.endswith("::User") for q in q_modified)
                # every change must belong to the edited file only, so
                # modules that were not touched stay untouched
                assert all(
                    "models/users.py" in rec["file"]
                    for kind in ("added", "modified", "removed")
                    for rec in diff["by_qn"][kind].values())
                result["change_set"] = json.dumps(
                    {k: sorted(r["name"] for r in v)
                     for k, v in diff["serialized"].items()},
                    ensure_ascii=False)

                # navigate to the AUTH module and read the highlight classes
                window.evaluate_js(
                    "(function(){var i=document.getElementById("
                    "'module-search');i.value='auth';"
                    "i.dispatchEvent(new Event('input',{bubbles:true}));"
                    "return 'ok';})()")
                time.sleep(0.8)
                window.evaluate_js(
                    "(function(){var it=document.querySelector("
                    "'#module-list .item');"
                    "it.dispatchEvent(new MouseEvent('mousedown',"
                    "{bubbles:true}));return 'ok';})()")
                time.sleep(2)
                result["hl_micro"] = window.evaluate_js(
                    "JSON.stringify({"
                    "view: window.__codemap_view,"
                    "selected: document.getElementById("
                    "'module-search').value,"
                    "modified: document.querySelectorAll("
                    "'#graph g.node.modifiedClass').length,"
                    "added: document.querySelectorAll("
                    "'#graph g.node.addedClass').length,"
                    "removed: document.querySelectorAll("
                    "'#graph g.node.removedClass').length,"
                    "last: JSON.stringify(window.__codemap_last_change)})")

                # ---- restore file + rebuild; 2 s mtime poll must notice ----
                users_path.write_bytes(orig_bytes)
                proc = subprocess.run(
                    [sys.executable, "-m", "code_review_graph", "build",
                     "--repo", REPO_ROOT, "-q"],
                    cwd=CODEMAP_ROOT, capture_output=True, text=True,
                    timeout=300)
                assert proc.returncode == 0, (proc.stderr or "")[-400:]
                restored_ok = True
                auto = None
                for _ in range(14):  # up to ~7 s (2 s poll + ~0.8 s update)
                    time.sleep(0.5)
                    raw = window.evaluate_js(
                        "JSON.stringify(window.__codemap_last_change)")
                    if raw and raw != "null":
                        auto = json.loads(raw)
                        if auto.get("auto") is True and auto.get("changed"):
                            break
                result["auto"] = json.dumps(auto, ensure_ascii=False)
                result["hl_auto"] = window.evaluate_js(
                    "JSON.stringify({"
                    "modified: document.querySelectorAll("
                    "'#graph g.node.modifiedClass').length,"
                    "added: document.querySelectorAll("
                    "'#graph g.node.addedClass').length,"
                    "removed: document.querySelectorAll("
                    "'#graph g.node.removedClass').length,"
                    "status: document.getElementById("
                    "'status').textContent})")

                # unchanged modules must carry no badges (macro view)
                window.evaluate_js(
                    "document.getElementById('view-macro').click(); 'ok'")
                time.sleep(2)
                mg = bridge.get_macro()
                result["macro_change"] = json.dumps(
                    {m["key"]: m.get("change")
                     for m in mg.get("modules", [])},
                    ensure_ascii=False)
                result["macro_status"] = window.evaluate_js(
                    "document.getElementById('status').textContent")
                result["hl_macro"] = window.evaluate_js(
                    "JSON.stringify((function(){"
                    "var graph=document.getElementById('graph');"
                    "var authNode=null,favNode=null;"
                    "graph.querySelectorAll('g.node').forEach(function(g){"
                    "var t=(g.textContent||'');"
                    "if(t.indexOf('认证模块')!==-1)authNode=t;"
                    "if(t.indexOf('收藏模块')!==-1)favNode=t;});"
                    "return {"
                    "authAdded: authNode?authNode.indexOf('+新增')!==-1:null,"
                    "authModified: authNode?authNode.indexOf('~修改')!==-1:null,"
                    "authRemoved: authNode?authNode.indexOf('-删除')!==-1:null,"
                    "favBadges: favNode?((favNode.indexOf('+新增')!==-1)"
                    "||(favNode.indexOf('~修改')!==-1)"
                    "||(favNode.indexOf('-删除')!==-1)):null};})())")

                # ---- in-app repo load: round-trip the default repo --------
                # load_repo must accept the same path as the hardcoded
                # REPO_ROOT, restore the fixed 5-module split, and hand back
                # a fresh macro without triggering a rebuild.
                lr = bridge.load_repo(REPO_ROOT)
                assert lr.get("status") == "ok", lr
                assert [m["key"] for m in lr["modules"]] == \
                    ["AUTH", "NEWS", "FAV", "HIST", "SUPPORT"], lr
                assert len(lr["macro"]["modules"]) == 5, lr
                result["load_repo_roundtrip"] = json.dumps(
                    {"status": lr["status"], "repo": lr["repo"],
                     "modules": [m["key"] for m in lr["modules"]],
                     "macro_modules": len(lr["macro"]["modules"]),
                     "macro_edges": len(lr["macro"]["edges"]),
                     "mtime": lr.get("mtime")}, ensure_ascii=False)

                # graph must be fully restored to the pre-edit baseline
                final_nodes = bridge._snapshot_nodes()
                restored_hash = next(
                    v["hash"] for q, v in final_nodes.items()
                    if q.rstrip("/").endswith("models/users.py"))
                assert len(final_nodes) == len(snap_before), (
                    "node count not restored: %d != %d"
                    % (len(final_nodes), len(snap_before)))
                assert restored_hash == base_users_hash, "users.py hash not restored"
                result["final_nodes"] = len(final_nodes)
                gp = subprocess.run(
                    ["git", "-C", REPO_ROOT, "status", "--porcelain"],
                    capture_output=True, text=True)
                result["git_clean"] = "users.py" not in gp.stdout
            finally:
                # whatever happened, leave the repo and graph as we found it
                try:
                    if users_path.read_bytes() != orig_bytes:
                        users_path.write_bytes(orig_bytes)
                    if not restored_ok:
                        subprocess.run(
                            [sys.executable, "-m", "code_review_graph",
                             "build", "--repo", REPO_ROOT, "-q"],
                            cwd=CODEMAP_ROOT, capture_output=True,
                            text=True, timeout=300)
                except Exception as exc2:  # pragma: no cover - cleanup only
                    result["cleanup_error"] = repr(exc2)
        except Exception as exc:  # pragma: no cover - depends on webview state
            result["error"] = repr(exc)
        finally:
            micro_window.destroy()
            window.destroy()

    def on_shown(*_args) -> None:
        _enable_borderless_resize(window)
        result["chrome"] = _window_chrome_flags(window)

    window.events.shown += on_shown
    window.events.loaded += on_loaded
    webview.start()

    print("SMOKE title:", result.get("title"))
    print("SMOKE status:", result.get("status"))
    print("SMOKE counts:", result.get("counts"))
    print("SMOKE macro:", result.get("macro"))
    print("SMOKE zoom0:", result.get("zoom0"))
    print("SMOKE zoom_in:", result.get("zoom_in"))
    print("SMOKE pan0:", result.get("pan0"))
    print("SMOKE pan1:", result.get("pan1"))
    print("SMOKE zoom_reset:", result.get("zoom_reset"))
    print("SMOKE micro:", result.get("micro"))
    print("SMOKE macro2:", result.get("macro2"))
    print("SMOKE switch_micro:", result.get("switch_micro"))
    print("SMOKE fuzzy_his:", result.get("fuzzy_his"))
    print("SMOKE pick_hist:", result.get("pick_hist"))
    print("SMOKE fuzzy_news:", result.get("fuzzy_news"))
    print("SMOKE switch_news:", result.get("switch_news"))
    print("SMOKE focus_open:", result.get("focus_open"))
    print("SMOKE fuzzy_user_his:", result.get("fuzzy_user_his"))
    print("SMOKE user_enter_hist:", result.get("user_enter_hist"))
    print("SMOKE focus_again:", result.get("focus_again"))
    print("SMOKE fuzzy_user_news:", result.get("fuzzy_user_news"))
    print("SMOKE user_enter_news:", result.get("user_enter_news"))
    print("SMOKE fuzzy_nomatch:", result.get("fuzzy_nomatch"))
    print("SMOKE drag_probe:", result.get("drag_probe"))
    print("SMOKE legend_overlap:", result.get("legend_overlap"))
    print("SMOKE pop:", result.get("pop"))
    print("SMOKE pop_visible:", result.get("pop_visible"))
    print("SMOKE micro_chrome:", result.get("micro_chrome"))
    print("SMOKE pop_hidden:", result.get("pop_hidden"))
    print("SMOKE pop2:", result.get("pop2"))
    print("SMOKE chrome:", result.get("chrome"))
    print("SMOKE dpr:", result.get("dpr"))
    print("SMOKE size before/after:", result.get("size_before"), "->", result.get("size_after"))
    print("SMOKE native rect before/after:", result.get("rect_before"), "->", result.get("rect_after"))
    print("SMOKE native rect final:", result.get("rect_final"))
    print("SMOKE change_set:", result.get("change_set"))
    print("SMOKE hl_micro:", result.get("hl_micro"))
    print("SMOKE auto:", result.get("auto"))
    print("SMOKE hl_auto:", result.get("hl_auto"))
    print("SMOKE hl_macro:", result.get("hl_macro"))
    print("SMOKE macro_change:", result.get("macro_change"))
    print("SMOKE load_repo_roundtrip:", result.get("load_repo_roundtrip"))
    print("SMOKE macro_status:", result.get("macro_status"))
    print("SMOKE final_nodes:", result.get("final_nodes"))
    print("SMOKE git_clean:", result.get("git_clean"))
    if result.get("cleanup_error"):
        print("SMOKE cleanup_error:", result["cleanup_error"])
    if result.get("error"):
        print("SMOKE error:", result["error"])
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="codemap_ui", description="CodeMap floating window (pywebview)")
    parser.add_argument("--db", default=None,
                        help="override graph.db path (default: config.DEFAULT_DB_PATH)")
    parser.add_argument("--debug", action="store_true",
                        help="open WebView2 devtools")
    parser.add_argument("--smoke-test", action="store_true",
                        help="open the window, verify the bridge, then close")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.db:
        os.environ["CODEMAP_DB_PATH"] = args.db

    bridge = Bridge()
    micro_bridge = MicroBridge()
    window = create_window(bridge)
    micro_window = create_micro_window(micro_bridge)
    bridge._micro_window = micro_window
    bridge._micro_bridge = micro_bridge

    # The native WinForms handle only exists once the window is shown, so
    # apply the borderless-resize style from the `shown` event rather than
    # immediately after create_window(). In smoke mode the smoke handler
    # applies the style itself and captures the resulting chrome flags.
    if not args.smoke_test:
        window.events.shown += lambda *_a: _enable_borderless_resize(window)
        micro_window.events.shown += lambda *_a: _enable_borderless_resize(micro_window)
        # Closing the main window exits the app: take the detached micro
        # window down with it (otherwise the hidden window keeps the process
        # alive with no visible UI).
        window.events.closed += lambda *_a: _safe_destroy(micro_window)
    if args.smoke_test:
        _smoke_test(bridge, window, micro_window, micro_bridge)
    else:
        webview.start()


if __name__ == "__main__":
    main()
