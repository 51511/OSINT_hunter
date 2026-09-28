"""調查結果的存取（JSON + Markdown）。"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .models import Investigation


def _slug(s: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", s.strip())[:40].strip("_")
    return s or "target"


def save(inv: Investigation, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{stamp}_{_slug(inv.target.name)}"
    path = directory / f"{base}.json"
    path.write_text(json.dumps(inv.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / f"{base}.md").write_text(inv.report_md or "", encoding="utf-8")
    return path


def load(path: Path) -> Investigation:
    return Investigation.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def list_saved(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(directory.glob("*.json"), reverse=True)
