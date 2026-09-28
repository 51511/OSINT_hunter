"""SearXNG 搜尋層：批次查詢、去重、限流退避、健康檢查。"""
from __future__ import annotations

import logging
import time
from typing import Callable

import requests

from .config import Settings
from .models import SearchHit

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0 Safari/537.36"


class SearXNG:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.s = settings
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept": "application/json"})
        self.base = settings.searxng_url.rstrip("/")

    # ---------- 健康檢查 ----------
    def health(self) -> dict:
        """回傳 {ok, latency_ms, error, json_enabled}。"""
        t0 = time.time()
        try:
            r = self.session.get(
                f"{self.base}/search",
                params={"q": "test", "format": "json"},
                timeout=self.s.searxng_timeout,
            )
            ms = int((time.time() - t0) * 1000)
            if r.status_code == 403:
                return {"ok": False, "latency_ms": ms, "json_enabled": False,
                        "error": "HTTP 403：SearXNG 未啟用 JSON 格式，請在 settings.yml 加入 search.formats: [html, json]"}
            r.raise_for_status()
            r.json()
            return {"ok": True, "latency_ms": ms, "json_enabled": True, "error": ""}
        except Exception as e:
            return {"ok": False, "latency_ms": int((time.time() - t0) * 1000),
                    "json_enabled": False, "error": str(e)}

    # ---------- 單次查詢 ----------
    def query(self, q: str, *, max_results: int | None = None, retries: int = 3) -> list[SearchHit]:
        max_results = max_results or self.s.max_results_per_query
        params = {
            "q": q,
            "format": "json",
            "language": self.s.searxng_language,
            "safesearch": 0,
        }
        if self.s.searxng_engines:
            params["engines"] = ",".join(self.s.searxng_engines)

        for attempt in range(retries):
            try:
                r = self.session.get(f"{self.base}/search", params=params,
                                     timeout=self.s.searxng_timeout)
                if r.status_code == 429:
                    wait = 2 ** (attempt + 1)
                    log.warning("SearXNG 限流 429，%ss 後重試：%s", wait, q)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                data = r.json()
                hits: list[SearchHit] = []
                for item in data.get("results", []):
                    url = (item.get("url") or "").strip()
                    if not url.startswith(("http://", "https://")):
                        continue
                    hits.append(SearchHit(
                        title=(item.get("title") or "").strip(),
                        url=url,
                        snippet=(item.get("content") or "").strip(),
                        engine=",".join(item.get("engines") or [item.get("engine", "")]),
                        query=q,
                    ))
                    if len(hits) >= max_results:
                        break
                return hits
            except requests.RequestException as e:
                log.warning("SearXNG 查詢失敗（%d/%d）%s：%s", attempt + 1, retries, q, e)
                time.sleep(1.5 * (attempt + 1))
            except ValueError as e:  # JSON 解析失敗
                log.error("SearXNG 回傳非 JSON：%s", e)
                return []
        return []

    # ---------- 批次 ----------
    def batch(self, queries: list[str], seen: set[str] | None = None,
              on_progress: Callable[[int, int, str], None] | None = None) -> tuple[list[SearchHit], int]:
        """依序查詢（刻意不平行，避免被 Google 限流）。回傳 (新結果, 原始總命中數)。"""
        seen = seen if seen is not None else set()
        fresh: list[SearchHit] = []
        raw_total = 0
        for i, q in enumerate(queries, 1):
            if on_progress:
                on_progress(i, len(queries), q)
            hits = self.query(q)
            raw_total += len(hits)
            for h in hits:
                k = h.key()
                if k in seen:
                    continue
                seen.add(k)
                fresh.append(h)
            if i < len(queries):
                time.sleep(self.s.search_qps_delay)
        return fresh, raw_total
