"""頁面抓取：robots.txt、SSRF 防護、大小限制、snippet-only 網域、論壇友善的文字抽取。"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from .config import Settings
from .models import Page, SearchHit, domain_of

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0 Safari/537.36"
ROBOTS_UA = "OSINTHunter"
MAX_DOWNLOAD_BYTES = 1_500_000
ALLOWED_CT = ("text/html", "application/xhtml+xml", "text/plain")


# ---------- SSRF 防護 ----------
def is_safe_url(url: str) -> tuple[bool, str]:
    """拒絕非 http(s)、內網、loopback、link-local、保留位址。"""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False, "非 http(s)"
    host = p.hostname
    if not host:
        return False, "無主機名稱"
    if host.lower().endswith((".onion", ".local", ".internal")):
        return False, "不允許的網域後綴"
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, "DNS 解析失敗"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False, f"解析到內部位址 {ip}"
    return True, ""


class _Robots:
    """每個網域快取一次 robots.txt。"""

    def __init__(self, timeout: int):
        self._cache: dict[str, RobotFileParser | None] = {}
        self._lock = threading.Lock()
        self.timeout = timeout

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        with self._lock:
            if base not in self._cache:
                rp = RobotFileParser()
                try:
                    r = requests.get(f"{base}/robots.txt", timeout=self.timeout,
                                     headers={"User-Agent": UA})
                    if r.status_code == 200:
                        rp.parse(r.text.splitlines())
                        self._cache[base] = rp
                    else:
                        self._cache[base] = None  # 無 robots → 允許
                except requests.RequestException:
                    self._cache[base] = None
            rp = self._cache[base]
        return True if rp is None else rp.can_fetch(ROBOTS_UA, url) or rp.can_fetch("*", url)


# ---------- 文字抽取 ----------
_NOISE_TAGS = ["script", "style", "noscript", "nav", "footer", "header", "aside", "form", "iframe", "svg"]


def extract_meta_profile(html: str) -> str:
    """從 og/twitter meta 抽出公開簡介（IG/Threads 未登入時偶爾仍有）。"""
    soup = BeautifulSoup(html, "lxml")
    bits: list[str] = []
    for prop in (
        "og:title", "og:description", "twitter:title", "twitter:description",
        "description",
    ):
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            bits.append(tag["content"].strip())
    title = soup.find("title")
    if title and title.get_text(strip=True):
        bits.append(title.get_text(strip=True))
    # 去重保序
    seen, out = set(), []
    for b in bits:
        if b and b not in seen:
            seen.add(b)
            out.append(b)
    return "\n".join(out)


def extract_text(html: str, max_chars: int) -> str:
    """抽出正文。保留區塊換行，讓論壇的「一則留言一行」結構不被壓扁。"""
    soup = BeautifulSoup(html, "lxml")
    for t in soup(_NOISE_TAGS):
        t.decompose()

    # PTT：推文在 .push；作者在 .article-metaline
    ptt_bits: list[str] = []
    for meta in soup.select(".article-metaline, .article-metaline-right"):
        ptt_bits.append(meta.get_text(" ", strip=True))
    for push in soup.select(".push"):
        ptt_bits.append(push.get_text(" ", strip=True))

    main = soup.find("article") or soup.find("main") or soup.body or soup
    text = main.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    if ptt_bits:
        text = "\n".join(ptt_bits) + "\n---\n" + text
    return text[:max_chars]


class Scraper:
    def __init__(self, settings: Settings):
        self.s = settings
        self.robots = _Robots(settings.scrape_timeout)
        self._local = threading.local()

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "s"):
            s = requests.Session()
            s.headers.update({"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
                              "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})
            # PTT 18 禁確認
            s.cookies.set("over18", "1", domain="www.ptt.cc")
            self._local.s = s
        return self._local.s

    def is_snippet_only(self, url: str) -> bool:
        d = domain_of(url)
        return any(d == x or d.endswith("." + x) for x in self.s.snippet_only_domains)

    def _is_profile_url(self, url: str) -> bool:
        """粗判是否像個人頁（值得嘗試抓 meta）。"""
        from .handles import from_url
        return from_url(url) is not None

    def scrape_one(self, hit: SearchHit) -> Page:
        snippet_text = f"{hit.title}\n{hit.snippet}".strip()
        snippet_only = self.is_snippet_only(hit.url)

        # snippet-only 網域：非個人頁直接放棄；個人頁仍嘗試抓 og meta（簡介）
        if snippet_only and not self._is_profile_url(hit.url):
            return Page(hit.url, hit.title, snippet_text, fetched=False, note="snippet-only 網域（需登入/反爬）")

        ok, why = is_safe_url(hit.url)
        if not ok:
            return Page(hit.url, hit.title, snippet_text, fetched=False, note=f"略過：{why}")

        if self.s.respect_robots and not self.robots.allowed(hit.url):
            return Page(hit.url, hit.title, snippet_text, fetched=False, note="robots.txt 禁止")

        resp = None
        try:
            resp = self._session().get(hit.url, timeout=(6, self.s.scrape_timeout), stream=True,
                                       allow_redirects=True)
            ok2, why2 = is_safe_url(resp.url)
            if not ok2:
                return Page(hit.url, hit.title, snippet_text, fetched=False, note=f"重新導向後略過：{why2}")
            if resp.status_code != 200:
                return Page(hit.url, hit.title, snippet_text, fetched=False, note=f"HTTP {resp.status_code}")
            ct = (resp.headers.get("Content-Type") or "").lower()
            if ct and not any(t in ct for t in ALLOWED_CT):
                return Page(hit.url, hit.title, snippet_text, fetched=False, note=f"不支援 {ct.split(';')[0]}")

            buf, n = [], 0
            for chunk in resp.iter_content(8192):
                if not chunk:
                    continue
                n += len(chunk)
                if n > MAX_DOWNLOAD_BYTES:
                    break
                buf.append(chunk)
            raw = b"".join(buf)
            html = raw.decode(resp.encoding or resp.apparent_encoding or "utf-8", errors="replace")

            if snippet_only:
                meta = extract_meta_profile(html)
                if meta and len(meta) >= 20:
                    text = f"{snippet_text}\n---\n[公開 meta/簡介]\n{meta}"
                    return Page(hit.url, hit.title, text, fetched=True, note="snippet-only 網域，僅取得 meta 簡介")
                return Page(hit.url, hit.title, snippet_text, fetched=False, note="snippet-only 網域（meta 亦無內容）")

            text = extract_text(html, self.s.scrape_max_chars)
            if len(text) < 80:
                meta = extract_meta_profile(html)
                if meta and len(meta) >= 20:
                    return Page(hit.url, hit.title, f"{snippet_text}\n---\n[公開 meta]\n{meta}",
                                fetched=True, note="正文空白，改用 meta")
                return Page(hit.url, hit.title, snippet_text, fetched=False, note="頁面近乎空白（可能需 JS）")
            return Page(hit.url, hit.title, f"{hit.title}\n{text}", fetched=True)
        except requests.RequestException as e:
            return Page(hit.url, hit.title, snippet_text, fetched=False, note=f"抓取失敗：{type(e).__name__}")
        finally:
            if resp is not None:
                resp.close()

    def scrape_many(self, hits: list[SearchHit]) -> dict[str, Page]:
        out: dict[str, Page] = {}
        if not hits:
            return out
        workers = max(1, min(self.s.scrape_threads, 16))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(self.scrape_one, h): h for h in hits}
            for f in as_completed(futs):
                h = futs[f]
                try:
                    out[h.url] = f.result()
                except Exception as e:  # 不讓單頁失敗拖垮整批
                    log.debug("scrape 例外 %s：%s", h.url, e)
                    out[h.url] = Page(h.url, h.title, f"{h.title}\n{h.snippet}", False, f"例外：{e}")
        return out
