"""真實 HTTP I/O 測試：本機起假 SearXNG + 假網站，讓真的 SearXNG/Scraper 類別走真網路。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from osint_hunter.config import Settings
from osint_hunter.models import SearchHit
from osint_hunter.search import SearXNG
from osint_hunter.scrape import Scraper

PAGE = "<html><body><article>" + "小明的設計日記，我住在台北，專注平面設計。" * 8 + "</article></body></html>"
state = {"searx_calls": 0, "fail_first": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/search":
            state["searx_calls"] += 1
            if state["fail_first"] > 0:
                state["fail_first"] -= 1
                self.send_response(429); self.end_headers(); return
            q = parse_qs(u.query).get("q", [""])[0]
            body = json.dumps({"results": [
                {"url": f"http://127.0.0.1:{self.server.server_port}/page", "title": f"結果 {q}",
                 "content": "摘要", "engines": ["google", "bing"]},
                {"url": "ftp://bad/x", "title": "壞協議", "content": ""},
            ]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(body)
        elif u.path == "/page":
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
            self.wfile.write(PAGE.encode())
        elif u.path == "/robots.txt":
            self.send_response(404); self.end_headers()
        elif u.path == "/forbidden":
            self.send_response(403); self.end_headers()
        else:
            self.send_response(404); self.end_headers()


@pytest.fixture(scope="module")
def server():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield srv
    srv.shutdown()


def mk(server):
    s = Settings(); s.searxng_url = f"http://127.0.0.1:{server.server_port}"; s.search_qps_delay = 0
    return s


def test_searx_query_parses_and_filters_bad_scheme(server):
    hits = SearXNG(mk(server)).query("小明 台北")
    assert len(hits) == 1                          # ftp:// 被濾掉
    assert hits[0].title == "結果 小明 台北"
    assert hits[0].engine == "google,bing"


def test_searx_retries_on_429(server):
    state["fail_first"] = 2
    hits = SearXNG(mk(server)).query("retry")
    assert len(hits) == 1                          # 前兩次 429，第三次成功


def test_searx_batch_dedup(server):
    seen = set()
    fresh, raw = SearXNG(mk(server)).batch(["a", "b", "c"], seen)
    assert raw == 3 and len(fresh) == 1            # 三個查詢回同一網址 → 去重成 1


def test_health_ok(server):
    assert SearXNG(mk(server)).health()["ok"] is True


def test_health_down():
    s = Settings(); s.searxng_url = "http://127.0.0.1:1"; s.searxng_timeout = 2
    h = SearXNG(s).health()
    assert h["ok"] is False and h["error"]


def test_scraper_ssrf_blocks_loopback_by_default(server):
    """預設 SSRF 防護：連 127.0.0.1 的頁面也必須被擋（這正是防護該有的行為）。"""
    s = mk(server)
    hit = SearchHit("x", f"http://127.0.0.1:{server.server_port}/page", "摘要")
    page = Scraper(s).scrape_one(hit)
    assert page.fetched is False and "內部位址" in page.note


def test_scraper_fetches_real_page_when_guard_bypassed(server, monkeypatch):
    """僅在測試中放行 loopback，驗證抓取/抽文字的真實 I/O 路徑。"""
    import osint_hunter.scrape as sc
    monkeypatch.setattr(sc, "is_safe_url", lambda url: (True, ""))
    s = mk(server); s.respect_robots = True
    hit = SearchHit("x", f"http://127.0.0.1:{server.server_port}/page", "摘要")
    page = Scraper(s).scrape_one(hit)
    assert page.fetched is True and "設計日記" in page.text


def test_scraper_http_error(server, monkeypatch):
    import osint_hunter.scrape as sc
    monkeypatch.setattr(sc, "is_safe_url", lambda url: (True, ""))
    hit = SearchHit("x", f"http://127.0.0.1:{server.server_port}/forbidden", "摘要")
    page = Scraper(mk(server)).scrape_one(hit)
    assert page.fetched is False and "403" in page.note


def test_snippet_only_domains_never_fetched():
    s = Settings()
    sc = Scraper(s)
    for u in ["https://www.instagram.com/x/", "https://threads.net/@a", "https://m.facebook.com/a", "https://x.com/a"]:
        p = sc.scrape_one(SearchHit("t", u, "snip"))
        assert p.fetched is False and "snippet-only" in p.note, u
