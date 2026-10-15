"""資料模型：搜尋結果、頁面內容、證據、候選帳號、調查狀態。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    engine: str = ""
    query: str = ""

    @property
    def domain(self) -> str:
        return domain_of(self.url)

    def key(self) -> str:
        return self.url.split("#")[0].rstrip("/").lower()


@dataclass
class Page:
    url: str
    title: str
    text: str
    fetched: bool  # False = 只有 snippet，沒有真的抓到內文
    note: str = ""  # 例如 "snippet-only domain" / "robots disallow" / "HTTP 403"


@dataclass
class Evidence:
    """一則可回溯的證據。報告中每個結論都必須指向至少一則。"""
    url: str
    quote: str  # 原文引用（不得杜撰，會被程式驗證是否真的存在於來源）
    verified: bool = False  # quote 是否確實出現在來源文字中


@dataclass
class Candidate:
    """候選帳號 / ID。"""
    platform: str
    handle: str
    profile_url: str = ""
    confidence: str = "low"  # high | medium | low
    reasoning: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    conflicts: str = ""  # 與目標不一致的地方 / 同名不同人的疑慮

    def ident(self) -> tuple[str, str]:
        return (self.platform.lower().strip(), self.handle.lower().lstrip("@").strip())


@dataclass
class RoundLog:
    round_no: int
    queries: list[str]
    hits: int
    new_hits: int
    filtered: int
    scraped: int
    new_handles: list[str] = field(default_factory=list)


@dataclass
class Target:
    """使用者輸入的目標與錨點。"""
    name: str
    anchors: str = ""  # 地區、職業、學校…
    known_handles: list[str] = field(default_factory=list)
    nicknames: list[str] = field(default_factory=list)  # 中文暱稱、別名

    def describe(self) -> str:
        parts = [f"名稱：{self.name}"]
        if self.nicknames:
            parts.append("暱稱/別名：" + "、".join(self.nicknames))
        if self.anchors.strip():
            parts.append(f"錨點資訊：{self.anchors.strip()}")
        if self.known_handles:
            parts.append("已知帳號：" + "、".join(self.known_handles))
        return "\n".join(parts)


@dataclass
class Investigation:
    target: Target
    model: str = ""
    started: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    rounds: list[RoundLog] = field(default_factory=list)
    hits: dict[str, SearchHit] = field(default_factory=dict)
    pages: dict[str, Page] = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    report_md: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": asdict(self.target),
            "model": self.model,
            "started": self.started,
            "rounds": [asdict(r) for r in self.rounds],
            "hits": [asdict(h) for h in self.hits.values()],
            "pages": [asdict(p) for p in self.pages.values()],
            "candidates": [asdict(c) for c in self.candidates],
            "report_md": self.report_md,
            "warnings": self.warnings,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Investigation":
        inv = cls(target=Target(**d["target"]), model=d.get("model", ""), started=d.get("started", ""))
        inv.rounds = [RoundLog(**r) for r in d.get("rounds", [])]
        for h in d.get("hits", []):
            hit = SearchHit(**h)
            inv.hits[hit.key()] = hit
        for p in d.get("pages", []):
            page = Page(**p)
            inv.pages[page.url] = page
        for c in d.get("candidates", []):
            ev = [Evidence(**e) for e in c.get("evidence", [])]
            inv.candidates.append(Candidate(**{**c, "evidence": ev}))
        inv.report_md = d.get("report_md", "")
        inv.warnings = d.get("warnings", [])
        return inv
