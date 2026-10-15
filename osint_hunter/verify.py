"""引文驗證：LLM 給的「逐字引文」必須真的出現在來源頁面。防幻覺的程式層最後一關。"""
from __future__ import annotations

import re
import unicodedata

from .models import Candidate, Evidence, Page


def _norm(s: str) -> str:
    """正規化：全形→半形、壓縮空白、去除零寬字元、轉小寫。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def quote_in_text(quote: str, text: str, *, min_len: int = 6) -> bool:
    """引文是否（在正規化後）出現於來源文字。允許引文被省略號截斷成多段。"""
    q = _norm(quote)
    if len(q) < min_len:
        return False
    t = _norm(text)
    if q in t:
        return True
    # 允許「…」「...」分隔的多段引文：每段都要存在且順序一致
    # 每段門檻較低（中文 3 字已有意義），但需至少 2 段，且各段合計長度要夠，避免短詞蒙混
    parts = [p.strip() for p in re.split(r"…|\.{3,}|⋯", q) if len(p.strip()) >= 3]
    if len(parts) >= 2 and sum(len(p) for p in parts) >= min_len:
        pos = 0
        for part in parts:
            i = t.find(part, pos)
            if i == -1:
                return False
            pos = i + len(part)
        return True
    return False


def verify_candidates(cands: list[Candidate], pages: dict[str, Page]) -> dict:
    """就地標記每則 evidence.verified，並依驗證結果調整信心。回傳統計。"""
    stats = {"evidence_total": 0, "evidence_verified": 0, "downgraded": 0}
    for c in cands:
        for ev in c.evidence:
            stats["evidence_total"] += 1
            page = pages.get(ev.url)
            ev.verified = bool(page and quote_in_text(ev.quote, page.text))
            if ev.verified:
                stats["evidence_verified"] += 1

        verified = sum(1 for e in c.evidence if e.verified)
        before = c.confidence

        # 沒有任何經驗證的證據 → 信心一律降為 low
        if verified == 0 and c.confidence != "low":
            c.confidence = "low"
            c.conflicts = (c.conflicts + "；" if c.conflicts else "") + "無經驗證的引文，信心已自動降級"
        # high 需要至少兩則經驗證的證據
        elif c.confidence == "high" and verified < 2:
            c.confidence = "medium"
            c.conflicts = (c.conflicts + "；" if c.conflicts else "") + "經驗證的證據不足兩則，由 high 降為 medium"

        # 只有 snippet（沒真的抓到頁面）的證據，不足以撐 high
        if c.confidence == "high":
            fetched_ok = sum(1 for e in c.evidence if e.verified and pages.get(e.url) and pages[e.url].fetched)
            if fetched_ok == 0:
                c.confidence = "medium"
                c.conflicts = (c.conflicts + "；" if c.conflicts else "") + "證據僅來自搜尋摘要，未取得完整頁面"

        if c.confidence != before:
            stats["downgraded"] += 1
    return stats


def leads_in_pages(leads: list[str], pages: dict[str, Page]) -> list[str]:
    """new_leads 必須逐字出現在某個頁面中，否則視為 LLM 杜撰而丟棄。"""
    big = _norm("\n".join(p.text for p in pages.values()))
    out, seen = [], set()
    for l in leads:
        l2 = (l or "").strip()
        if len(l2) < 2 or l2.lower() in seen:
            continue
        if _norm(l2) in big:
            seen.add(l2.lower())
            out.append(l2)
    return out
