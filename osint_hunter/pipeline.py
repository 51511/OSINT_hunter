"""調查引擎：多輪「展開查詢 → 搜尋 → 篩選 → 抓取 → 抽取 ID → 評分 → 新線索反查」迴圈。"""
from __future__ import annotations

import json
import logging
from typing import Callable

from . import handles as H
from . import prompts as P
from .config import Settings
from .llm import LLM, LLMError
from .models import Candidate, Evidence, Investigation, Page, RoundLog, SearchHit, Target
from .scrape import Scraper
from .search import SearXNG
from .verify import leads_in_pages, verify_candidates

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, str], None]  # (stage, message)


def _noop(stage: str, msg: str) -> None:
    pass


class Investigator:
    def __init__(self, settings: Settings, llm: LLM, searx: SearXNG | None = None,
                 scraper: Scraper | None = None, progress: ProgressFn = _noop):
        self.s = settings
        self.llm = llm
        self.searx = searx or SearXNG(settings)
        self.scraper = scraper or Scraper(settings)
        self.progress = progress

    # ─────────── 步驟 1：查詢展開 ───────────
    def expand_queries(self, target: Target, done: list[str], leads: list[str], n: int) -> list[str]:
        system = P.render(P.EXPAND_SYSTEM, n=n)
        user = f"【目標】\n{target.describe()}\n"
        if leads:
            user += "\n【上一輪發現的新線索（請優先用來反查）】\n" + "\n".join(f"- {l}" for l in leads) + "\n"
        if done:
            user += "\n【已搜尋過的查詢（不要重複）】\n" + "\n".join(f"- {q}" for q in done[-40:]) + "\n"
        data = self.llm.complete_json(system, user)
        raw = data.get("queries", data) if isinstance(data, dict) else data
        seen_q = {q.lower() for q in done}
        out: list[str] = []

        # 強制注入：全網人名/公開紀錄優先，社群與已知帳號其次
        forced: list[str] = []
        name = (target.name or "").strip()
        anchors = (target.anchors or "").replace("、", " ").replace(",", " ").strip()
        anchor_bits = [a for a in anchors.split() if a][:3]
        open_web: list[str] = []
        if name:
            open_web += [
                f'"{name}"',
                f'"{name}" 得獎',
                f'"{name}" 學校',
                f'"{name}" site:gov.tw',
                f'"{name}" site:edu.tw',
            ]
            if anchor_bits:
                open_web.append(f'"{name}" {" ".join(anchor_bits[:2])}')
        for nick in target.nicknames:
            nick = nick.strip()
            if not nick:
                continue
            open_web.append(f'"{nick}" {name}'.strip())
            open_web.append(f'"{name}" "{nick}"')
            if anchor_bits:
                open_web.append(f'"{nick}" {anchor_bits[0]}')
        for kh in target.known_handles:
            h = kh.lstrip("@").strip()
            if not h:
                continue
            open_web.append(f'"{h}"')
            open_web.append(f"site:instagram.com {h}")
            open_web.append(f"site:threads.net {h}")
            if name:
                open_web.append(f"{h} {name}")
        for q in open_web:
            q = " ".join(q.split())
            ql = q.lower()
            if ql not in seen_q and 2 <= len(q) <= 200:
                seen_q.add(ql)
                forced.append(q)

        for q in raw if isinstance(raw, list) else []:
            if not isinstance(q, str):
                continue
            q = " ".join(q.split())
            if 2 <= len(q) <= 200 and q.lower() not in seen_q:
                seen_q.add(q.lower())
                out.append(q)

        combined = forced + out
        return combined[:n]

    # ─────────── 步驟 2：LLM 篩選 ───────────
    def filter_hits(self, target: Target, hits: list[SearchHit], k: int) -> list[SearchHit]:
        if not hits:
            return []
        if len(hits) <= k:
            return hits

        # 優先：已知帳號命中、非社群公開頁（edu/gov/新聞/論壇）
        known = {x.lower().lstrip("@") for x in target.known_handles if x.strip()}
        nicks = {x.lower() for x in target.nicknames if x.strip()}
        social = ("instagram.com", "threads.net", "facebook.com", "fb.com", "tiktok.com")
        priority: list[SearchHit] = []
        public: list[SearchHit] = []
        rest: list[SearchHit] = []
        for h in hits:
            blob = f"{h.url} {h.title} {h.snippet}".lower()
            dom = (h.domain or "").lower()
            if known and any(kh in blob for kh in known):
                priority.append(h)
            elif any(s in dom for s in social):
                rest.append(h)
            elif (
                (target.name and target.name in blob)
                or any(n in blob for n in nicks)
                or any(x in dom for x in ("edu.tw", "gov.tw", "ptt.cc", "dcard.tw", "pixnet"))
            ):
                public.append(h)
            else:
                rest.append(h)
        priority = priority + public

        # 只送前 25 筆、短摘要，避免撐爆 16k context
        lines = []
        for i, h in enumerate(hits[:25], 1):
            snip = (h.snippet or "").replace("\n", " ")[:80]
            lines.append(f"{i}. [{h.domain}] {h.title[:60]} — {snip}")
        system = P.render(P.FILTER_SYSTEM, k=k)
        user = f"【目標】\n{target.describe()}\n\n【搜尋結果】\n" + "\n".join(lines)
        chosen: list[SearchHit] = []
        try:
            data = self.llm.complete_json(system, user)
            idx = data.get("keep", []) if isinstance(data, dict) else data
            seen: set[int] = set()
            for i in idx:
                try:
                    n = int(i)
                except (TypeError, ValueError):
                    continue
                if 1 <= n <= len(hits) and n not in seen:
                    seen.add(n)
                    chosen.append(hits[n - 1])
        except LLMError as e:
            log.warning("LLM 篩選失敗，改用啟發式：%s", e)

        # 合併：priority 在前，再補 LLM 選的，最後 rest
        out, seen_url = [], set()
        for h in priority + chosen + rest:
            key = h.key()
            if key in seen_url:
                continue
            seen_url.add(key)
            out.append(h)
            if len(out) >= k:
                break
        return out[:k] if out else hits[:k]

    # ─────────── 步驟 3：程式抽 ID（不靠 LLM）───────────
    def harvest_handles(
        self,
        hits: list[SearchHit],
        pages: dict[str, Page],
        known: list[str] | None = None,
    ) -> dict[tuple, H.Handle]:
        found: dict[tuple, H.Handle] = {}

        # 1) 一般從 URL / 內文抽取
        for h in hits:
            hd = H.from_url(h.url)
            if hd:
                found.setdefault(hd.ident(), hd)
        for url, page in pages.items():
            for hd in H.extract_all(url, page.text):
                found.setdefault(hd.ident(), hd)

        # 2) 已知帳號：只要出現在任何 hit 的 URL/標題/摘要 或 頁面文字，就納入
        if known:
            for key, hd in H.extract_known_from_hits(known, hits, pages).items():
                found.setdefault(key, hd)
            # 3) 即使搜尋結果沒直接命中，也把使用者提供的 known 預先 seed 進去
            #    （讓 LLM 有機會在 snippet 裡找錨點吻合證據）
            for key, hd in H.seed_known_handles(known).items():
                found.setdefault(key, hd)

        return found

    # ─────────── 步驟 4：LLM 評分（受限於程式抽出的 ID）───────────
    def score(self, target: Target, pages: dict[str, Page],
              handles: dict[tuple, H.Handle]) -> tuple[list[Candidate], list[str]]:
        if not handles:
            return [], []
        # 已知帳號 / known 平台排前面，方便 LLM 優先評估
        known_set = {k.lower().lstrip("@") for k in target.known_handles}
        ordered = sorted(
            handles.values(),
            key=lambda h: (0 if h.handle.lower() in known_set or h.platform == "known" else 1, h.platform, h.handle),
        )
        # 小 context（如 16k）必須嚴格控字數：handles≤20、每頁≤800 字、總頁面預算~6k 字
        hlines = [f"- {h.platform} / {h.handle} / {h.url}" for h in ordered][:20]
        per_page = min(800, self.s.scrape_max_chars)
        budget = 6000
        blocks: list[str] = []

        def _page_rank(item: tuple[str, Page]) -> tuple:
            url, p = item
            blob = (p.text or "").lower()
            hit_known = 1 if known_set and any(k in blob or k in url.lower() for k in known_set) else 0
            return (-hit_known, 0 if p.fetched else 1, -len(p.text or ""))

        for url, p in sorted(pages.items(), key=_page_rank):
            body = (p.text or "")[:per_page]
            tag = "完整頁面" if p.fetched else "僅搜尋摘要"
            blk = f"### URL: {url}\n（{tag}）\n{body}\n"
            if budget - len(blk) < 0:
                break
            budget -= len(blk)
            blocks.append(blk)
        system = P.render(P.EXTRACT_SYSTEM, max_leads=self.s.max_new_handles_per_round)
        user = (f"【目標】\n{target.describe()}\n\n【程式確定抽出的候選 ID】\n" + "\n".join(hlines)
                + "\n\n【已抓取頁面】\n" + "\n".join(blocks))
        data = self.llm.complete_json(system, user, max_tokens=min(2048, self.s.llm_max_tokens))
        valid = {h.ident() for h in handles.values()}
        cands: list[Candidate] = []
        for c in (data.get("candidates", []) if isinstance(data, dict) else []):
            try:
                plat = str(c.get("platform", "")).strip()
                hd = str(c.get("handle", "")).strip()
                # 硬約束：LLM 只能評估程式抽出的 ID，自行新增的一律丟棄
                if (plat.lower(), hd.lower().lstrip("@")) not in valid:
                    log.info("丟棄 LLM 自行新增的 ID：%s/%s", plat, hd)
                    continue
                conf = str(c.get("confidence", "low")).lower()
                if conf not in ("high", "medium", "low"):
                    conf = "low"
                ev = [Evidence(url=str(e.get("url", "")), quote=str(e.get("quote", "")))
                      for e in c.get("evidence", []) if isinstance(e, dict)]
                cands.append(Candidate(platform=plat, handle=hd.lstrip("@"),
                                       profile_url=str(c.get("profile_url", "")),
                                       confidence=conf, reasoning=str(c.get("reasoning", "")),
                                       evidence=ev, conflicts=str(c.get("conflicts", ""))))
            except Exception as e:
                log.debug("略過格式錯誤的候選：%s", e)
        leads = data.get("new_leads", []) if isinstance(data, dict) else []
        leads = [str(x) for x in leads if isinstance(x, (str, int))]
        return cands, leads

    # ─────────── 已知帳號後備：搜尋結果有命中就建候選（不依賴 LLM）───────────
    def fallback_known_candidates(
        self,
        target: Target,
        hits: list[SearchHit],
        pages: dict[str, Page],
        handles: dict[tuple, H.Handle],
    ) -> list[Candidate]:
        """當 LLM 評分交白卷時，只要已知帳號出現在 hit 的 URL/標題/摘要，就建立候選。

        信心最高 medium（證據多半來自 snippet）。這保證 -k 給的帳號不會被小模型直接吞掉。
        """
        known = [k.lstrip("@").strip() for k in target.known_handles if k.strip()]
        if not known:
            return []

        out: list[Candidate] = []
        seen: set[tuple[str, str]] = set()

        for kh in known:
            kh_l = kh.lower()
            matched_hits = [
                h for h in hits
                if kh_l in (h.url or "").lower()
                or kh_l in (h.title or "").lower()
                or kh_l in (h.snippet or "").lower()
            ]
            if not matched_hits:
                # 仍用 seed 的 handle（有 canonical URL）建一個 low 候選
                for key, hd in handles.items():
                    if hd.handle.lower() == kh_l and hd.platform != "known":
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append(Candidate(
                            platform=hd.platform,
                            handle=hd.handle,
                            profile_url=hd.url,
                            confidence="low",
                            reasoning="使用者提供的已知帳號；本輪搜尋摘要未直接命中，僅保留 canonical 連結供核對。",
                            evidence=[],
                            conflicts="搜尋摘要未出現此帳號，請手動開啟連結確認",
                        ))
                        break
                continue

            # 依平台分組，優先用 URL 規則對上的
            by_plat: dict[str, list[SearchHit]] = {}
            for h in matched_hits:
                hd = H.from_url(h.url)
                plat = hd.platform if hd and hd.handle.lower() == kh_l else "known"
                if plat == "known":
                    host = (h.url or "").lower()
                    for p in ("instagram", "threads", "facebook", "x", "github", "tiktok"):
                        if p in host or (p == "x" and "twitter.com" in host):
                            plat = "x" if p == "x" and "twitter" in host else p
                            break
                by_plat.setdefault(plat, []).append(h)

            for plat, group in by_plat.items():
                key = (plat, kh_l)
                if key in seen:
                    continue
                seen.add(key)
                # 選最像 profile 的 URL
                best = group[0]
                for h in group:
                    path = (h.url or "").rstrip("/")
                    if path.lower().endswith(kh_l) or f"/@{kh_l}" in path.lower():
                        best = h
                        break
                hd = handles.get((plat, kh_l))
                profile = (hd.url if hd and hd.url else "") or best.url
                # 從 title+snippet 取短引文
                quote = f"{best.title} {best.snippet}".strip()
                quote = quote[:120] if quote else kh
                conf = "medium" if any(
                    a and a in f"{best.title} {best.snippet}"
                    for a in (target.name, *(target.anchors.replace("、", " ").replace(",", " ").split()))
                    if a.strip()
                ) else "low"
                # 名字或錨點有出現 → medium；否則 low（但至少列出）
                if target.name and target.name in f"{best.title} {best.snippet}":
                    conf = "medium"
                out.append(Candidate(
                    platform=plat,
                    handle=kh,
                    profile_url=profile,
                    confidence=conf,
                    reasoning=(
                        f"搜尋結果命中已知帳號（{len(group)} 筆）。"
                        + ("標題/摘要含目標姓名或錨點。" if conf == "medium" else "僅帳號命中，未見姓名/錨點，請人工核對。")
                    ),
                    evidence=[Evidence(url=best.url, quote=quote)],
                    conflicts="" if conf == "medium" else "摘要未同時出現目標姓名與帳號，同名風險需人工確認",
                ))
        return out

    # ─────────── 合併候選（跨輪）───────────
    @staticmethod
    def merge(existing: list[Candidate], new: list[Candidate]) -> None:
        idx = {c.ident(): c for c in existing}
        rank = {"low": 0, "medium": 1, "high": 2}
        for n in new:
            cur = idx.get(n.ident())
            if not cur:
                existing.append(n)
                idx[n.ident()] = n
                continue
            have = {(e.url, e.quote) for e in cur.evidence}
            cur.evidence += [e for e in n.evidence if (e.url, e.quote) not in have]
            if rank[n.confidence] > rank[cur.confidence]:
                cur.confidence, cur.reasoning = n.confidence, n.reasoning
            if n.conflicts and n.conflicts not in cur.conflicts:
                cur.conflicts = (cur.conflicts + "；" if cur.conflicts else "") + n.conflicts
            cur.profile_url = cur.profile_url or n.profile_url

    # ─────────── 主流程 ───────────
    def run(self, target: Target, max_rounds: int | None = None) -> Investigation:
        s = self.s
        max_rounds = max_rounds or s.max_rounds
        inv = Investigation(target=target, model=f"{self.llm.provider}:{self.llm.model}")
        seen_urls: set[str] = set()
        done_queries: list[str] = []
        leads: list[str] = []
        used_leads: set[str] = {x.lower() for x in target.known_handles}

        h = self.searx.health()
        if not h["ok"]:
            inv.warnings.append(f"SearXNG 不可用：{h['error']}")
            self.progress("error", f"SearXNG 不可用：{h['error']}")
            return inv

        for rnd in range(1, max_rounds + 1):
            self.progress("round", f"第 {rnd}/{max_rounds} 輪")

            # 1 展開
            self.progress("expand", "LLM 產生查詢…")
            try:
                queries = self.expand_queries(target, done_queries, leads, s.queries_per_round)
            except LLMError as e:
                inv.warnings.append(f"第 {rnd} 輪查詢展開失敗：{e}")
                break
            if not queries:
                self.progress("info", "沒有新的查詢可用，結束。")
                break
            done_queries += queries
            self.progress("queries", json.dumps(queries, ensure_ascii=False))

            # 2 搜尋
            fresh, raw_total = self.searx.batch(
                queries, seen_urls,
                on_progress=lambda i, n, q: self.progress("search", f"[{i}/{n}] {q}"))
            for hit in fresh:
                inv.hits[hit.key()] = hit
            self.progress("info", f"命中 {raw_total}，新增 {len(fresh)}")
            if not fresh:
                inv.rounds.append(RoundLog(rnd, queries, raw_total, 0, 0, 0))
                if rnd > 1:
                    break
                continue

            # 3 篩選
            pool = fresh[: s.max_results_to_filter]
            # 第一輪：強制把已知帳號的 canonical profile URL 塞進 pool，確保會去抓 meta 簡介
            if rnd == 1 and target.known_handles:
                for key, hd in H.seed_known_handles(target.known_handles).items():
                    if hd.platform in ("instagram", "threads", "facebook", "x", "github") and hd.url:
                        syn = SearchHit(
                            title=f"{hd.platform} / {hd.handle}",
                            url=hd.url,
                            snippet=f"known profile {hd.handle}",
                            engine="seed",
                            query="known-handle-seed",
                        )
                        if syn.key() not in {h.key() for h in pool}:
                            pool.insert(0, syn)
            self.progress("filter", f"LLM 從 {len(pool)} 筆挑選…")
            chosen = self.filter_hits(target, pool, s.max_pages_to_scrape)

            # 4 抓取
            self.progress("scrape", f"抓取 {len(chosen)} 頁…")
            pages = self.scraper.scrape_many(chosen)
            # 未被選中的結果：只保留「含已知帳號」或前 8 筆摘要，避免撐爆 LLM context
            known_l = {x.lower().lstrip("@") for x in target.known_handles if x.strip()}
            extra = 0
            for hit in pool:
                if hit.url in pages:
                    continue
                blob = f"{hit.url} {hit.title} {hit.snippet}".lower()
                is_known = bool(known_l and any(k in blob for k in known_l))
                if is_known or extra < 8:
                    pages[hit.url] = Page(
                        hit.url, hit.title, f"{hit.title}\n{hit.snippet}", False, "未抓取，僅摘要"
                    )
                    if not is_known:
                        extra += 1
            inv.pages.update(pages)
            fetched = sum(1 for p in pages.values() if p.fetched)

            # 5 抽 ID + 評分（含已知帳號 seed）
            handles = self.harvest_handles(pool, pages, known=target.known_handles)
            self.progress("score", f"程式抽出 {len(handles)} 個候選 ID，LLM 評分中…")
            try:
                cands, new_leads = self.score(target, pages, handles)
            except LLMError as e:
                inv.warnings.append(f"第 {rnd} 輪評分失敗：{e}")
                cands, new_leads = [], []

            # 5b 後備：LLM 沒交候選時，用搜尋命中把已知帳號寫進結果
            have = {c.ident() for c in cands}
            for fb in self.fallback_known_candidates(target, pool, pages, handles):
                if fb.ident() not in have:
                    cands.append(fb)
                    have.add(fb.ident())
            # 也補上 LLM 有評但漏掉的其他 known 平台命中
            if target.known_handles and not any(
                c.handle.lower().lstrip("@") in {k.lower().lstrip("@") for k in target.known_handles}
                for c in cands
            ):
                for fb in self.fallback_known_candidates(target, pool, pages, handles):
                    if fb.ident() not in have:
                        cands.append(fb)
                        have.add(fb.ident())

            # 6 驗證引文（用全部累積頁面）
            stats = verify_candidates(cands, inv.pages)
            self.progress("verify", f"引文驗證 {stats['evidence_verified']}/{stats['evidence_total']}，降級 {stats['downgraded']}")
            self.merge(inv.candidates, cands)

            # 7 新線索（必須逐字存在於頁面）
            good = [l for l in leads_in_pages(new_leads, inv.pages) if l.lower() not in used_leads]
            good = good[: s.max_new_handles_per_round]
            used_leads |= {l.lower() for l in good}
            leads = good

            inv.rounds.append(RoundLog(rnd, queries, raw_total, len(fresh), len(chosen), fetched, good))
            # 有已知帳號時，第一輪就算沒新線索也至少跑完評分；之後沒新線索再結束
            if not good and rnd > 1:
                self.progress("info", "沒有新線索，提前結束。")
                break
            if not good and rnd == 1 and not target.known_handles:
                self.progress("info", "沒有新線索，提前結束。")
                break

        # 8 報告（小模型常在這步卡住 → 預設用程式報告，可選 LLM）
        self.progress("info", "產生報告…")
        inv.report_md = self.report(inv)
        return inv

    # ─────────── 報告 ───────────
    def report(self, inv: Investigation, stream: bool = False) -> str:
        rank = {"high": 0, "medium": 1, "low": 2}
        cands = sorted(inv.candidates, key=lambda c: rank.get(c.confidence, 9))
        # 本地小模型寫長報告又慢又容易掛；直接用結構化後備報告更穩
        use_llm = self.llm.provider in ("anthropic", "openai") and bool(cands)
        if not use_llm:
            return self.fallback_report(inv, cands)

        payload = {
            "target": inv.target.describe(),
            "rounds": [{"round": r.round_no, "queries": r.queries[:6], "new_hits": r.new_hits,
                        "scraped": r.scraped, "new_leads": r.new_handles} for r in inv.rounds],
            "candidates": [{
                "platform": c.platform, "handle": c.handle, "profile_url": c.profile_url,
                "confidence": c.confidence, "reasoning": (c.reasoning or "")[:200],
                "conflicts": (c.conflicts or "")[:120],
                "evidence": [{"url": e.url, "quote": (e.quote or "")[:80], "verified": e.verified}
                             for e in c.evidence[:3]],
            } for c in cands[:12]],
            "warnings": inv.warnings[:8],
        }
        user = "【已驗證的結構化結果】\n" + json.dumps(payload, ensure_ascii=False)
        try:
            out = self.llm.complete(
                P.render(P.REPORT_SYSTEM), user, stream=stream,
                max_tokens=min(1500, self.s.llm_max_tokens),
            )
            return out if (out or "").strip() else self.fallback_report(inv, cands)
        except Exception as e:
            inv.warnings.append(f"報告生成失敗：{e}")
            return self.fallback_report(inv, cands)

    @staticmethod
    def fallback_report(inv: Investigation, cands: list[Candidate]) -> str:
        """純程式報告：候選 + 含姓名的搜尋命中，不依賴 LLM。"""
        L = ["## 調查目標", inv.target.describe(), "", "## 候選帳號"]
        if not cands:
            L.append("- 未找到候選帳號（或 LLM 評分未回傳；請看下方搜尋命中）")
        for c in cands:
            L.append(f"- **{c.platform} / {c.handle}**（信心：{c.confidence}） {c.profile_url}")
            if c.reasoning:
                L.append(f"  - 判斷：{c.reasoning}")
            if c.conflicts:
                L.append(f"  - 疑慮：{c.conflicts}")
            for e in c.evidence:
                tag = "已驗證" if e.verified else "未驗證"
                L.append(f"  - 證據（{tag}）：「{e.quote}」— {e.url}")

        # 列出標題/摘要含姓名或暱稱的搜尋結果，方便人工點開
        name = (inv.target.name or "").lower()
        nicks = [n.lower() for n in inv.target.nicknames if n]
        L += ["", "## 相關搜尋命中（供人工核對）"]
        shown = 0
        for h in inv.hits.values():
            blob = f"{h.title} {h.snippet}".lower()
            if name and name in blob or any(n in blob for n in nicks):
                L.append(f"- [{h.domain}] {h.title[:100]}")
                L.append(f"  {h.url}")
                if h.snippet:
                    L.append(f"  > {(h.snippet or '')[:120]}")
                shown += 1
                if shown >= 15:
                    break
        if shown == 0:
            L.append("- （標題/摘要中未直接出現姓名；請到 investigations JSON 查看全部 hits）")

        L += [
            "", "## 調查過程",
            *(f"- 第 {r.round_no} 輪：查詢 {len(r.queries)}、新命中 {r.new_hits}、抓取 {r.scraped}"
              for r in inv.rounds),
            "", "## 限制與風險",
            "- IG/Threads/FB 多半只能看搜尋摘要或 meta，無法保證讀到貼文與留言。",
            "- 同名同姓可能誤判；請自行打開上方連結交叉驗證。",
            "- 本報告由程式彙整，未再經 LLM 改寫。",
        ]
        if inv.warnings:
            L += ["", "## 警告"] + [f"- {w}" for w in inv.warnings]
        return "\n".join(L)
