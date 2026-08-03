"""Window and data-source configuration for the CodeMap floating shell."""

from __future__ import annotations

import os

APP_TITLE = "CodeMap"

# Window geometry / chrome.
WINDOW_WIDTH = 900
WINDOW_HEIGHT = 700
MIN_WIDTH = 400
MIN_HEIGHT = 300
FRAMELESS = True
ON_TOP = True
EASY_DRAG = True
RESIZABLE = True

# CRG graph database. Override at runtime with CODEMAP_DB_PATH or --db.
DEFAULT_DB_PATH = r"E:\BaiduNetdiskDownload\code\toutiao_backend\.code-review-graph\graph.db"


def get_db_path() -> str:
    """Return the graph.db path, honouring the CODEMAP_DB_PATH override."""
    return os.environ.get("CODEMAP_DB_PATH", DEFAULT_DB_PATH)
