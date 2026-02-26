"""Shared test helpers for focused-review tests."""

from __future__ import annotations

import os
from pathlib import Path


def create_file(base: Path, rel: str, content: str = "") -> Path:
    """Create a file at *base/rel* with the given content."""
    p = base / rel.replace("/", os.sep)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p
