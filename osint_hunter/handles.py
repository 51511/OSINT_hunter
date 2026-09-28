"""從網址與文字中「用程式」抽取帳號 ID（不靠 LLM，避免幻覺）。

回傳的每個 handle 都保證確實出現在來源網址/文字中。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class Handle:
    platform: str
    handle: str
    url: str = ""

    def ident(self) -> tuple[str, str]:
        return (self.platform.lower(), self.handle.lower().lstrip("@"))


# 各平台個人頁網址規則：(platform, 網域正則, 路徑正則)
# Instagram：也接受 /username/ 後接查詢參數或尾隨斜線；排除明顯非個人頁
_URL_RULES: list[tuple[str, re.Pattern, re.Pattern]] = [
    ("instagram", re.compile(r"(^|\.)instagram\.com$"), re.compile(r"^/(?!p/|reel/|reels/|explore/|stories/|accounts/|tv/|tags/|directory/)([A-Za-z0-9._]{1,30})/?")),
    ("threads",   re.compile(r"(^|\.)threads\.(net|com)$"), re.compile(r"^/@([A-Za-z0-9._]{1,30})")),
    ("x",         re.compile(r"(^|\.)(twitter|x)\.com$"), re.compile(r"^/(?!i/|home|search|hashtag|explore|intent|share)([A-Za-z0-9_]{1,15})(/|$)")),
    ("facebook",  re.compile(r"(^|\.)facebook\.com$"), re.compile(r"^/(?!pages/|groups/|events/|watch|photo|share|profile\.php|marketplace|people/)([A-Za-z0-9.]{5,50})/?$")),
    ("github",    re.compile(r"(^|\.)github\.com$"), re.compile(r"^/(?!orgs/|topics/|search|features|pricing|about)([A-Za-z0-9-]{1,39})/?$")),
    ("tiktok",    re.compile(r"(^|\.)tiktok\.com$"), re.compile(r"^/@([A-Za-z0-9._]{1,24})")),
    ("youtube",   re.compile(r"(^|\.)youtube\.com$"), re.compile(r"^/@([A-Za-z0-9._-]{1,30})")),
    ("reddit",    re.compile(r"(^|\.)reddit\.com$"), re.compile(r"^/(?:user|u)/([A-Za-z0-9_-]{3,20})")),
    ("dcard",     re.compile(r"(^|\.)dcard\.tw$"), re.compile(r"^/@([A-Za-z0-9._-]{1,30})")),
    ("medium",    re.compile(r"(^|\.)medium\.com$"), re.compile(r"^/@([A-Za-z0-9._-]{1,30})")),
    ("linkedin",  re.compile(r"(^|\.)linkedin\.com$"), re.compile(r"^/in/([A-Za-z0-9%_-]{3,100})")),
    ("pixnet",    re.compile(r"^([A-Za-z0-9_-]{3,30})\.pixnet\.net$"), re.compile(r".*")),
    ("plurk",     re.compile(r"(^|\.)plurk\.com$"), re.compile(r"^/([A-Za-z0-9_]{3,30})/?$")),
    ("bahamut",   re.compile(r"^home\.gamer\.com\.tw$"), re.compile(r"^/(?:homeindex\.php)?.*")),
]

# 常見平台的 canonical profile URL 模板（用於已知帳號預先注入）
_PROFILE_TEMPLATES: dict[str, str] = {
    "instagram": "https://www.instagram.com/{h}/",
    "threads":   "https://www.threads.net/@{h}",
    "x":         "https://x.com/{h}",
    "twitter":   "https://x.com/{h}",
    "facebook":  "https://www.facebook.com/{h}",
    "github":    "https://github.com/{h}",
    "tiktok":    "https://www.tiktok.com/@{h}",
    "youtube":   "https://www.youtube.com/@{h}",
    "dcard":     "https://www.dcard.tw/@{h}",
    "plurk":     "https://www.plurk.com/{h}",
    "reddit":    "https://www.reddit.com/user/{h}",
    "medium":    "https://medium.com/@{h}",
}

# 論壇內文中的「ID」樣式
_PTT_PUSH = re.compile(r"^\s*[推噓→]\s*([A-Za-z][A-Za-z0-9]{1,11})\s*[:：]", re.M)
_PTT_AUTHOR = re.compile(r"作者\s*[:：]?\s*([A-Za-z][A-Za-z0-9]{1,11})\s*(?:\(|（)")
_AT_MENTION = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9._]{3,30})\b")
# 從搜尋標題/摘要中抓「像帳號」的英數底線字串（較寬鬆，之後靠 known 過濾）
_HANDLE_LIKE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9._]{2,29})(?![A-Za-z0-9_])")

_BAD_HANDLES = {
    "explore", "accounts", "p", "reel", "reels", "stories", "share", "login", "signup",
    "home", "search", "about", "help", "privacy", "terms", "www", "static", "assets",
    "instagram", "threads", "facebook", "twitter", "youtube", "tiktok", "github",
    "google", "index", "profile", "user", "users", "page", "pages", "post", "posts",
}


def _clean(h: str) -> str:
    return unquote(h).strip().strip("/").strip(".")


def from_url(url: str) -> Handle | None:
    """從單一網址抽出個人頁 ID；不是個人頁則回傳 None。"""
    try:
        p = urlparse(url)
    except ValueError:
        return None
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = unquote(p.path or "/")
    for platform, dom_re, path_re in _URL_RULES:
        m_dom = dom_re.search(host)
        if not m_dom:
            continue
        if platform == "pixnet":
            h = m_dom.group(1)
        elif platform == "bahamut":
            q = re.search(r"owner=([A-Za-z0-9_]{3,20})", p.query or "")
            if not q:
                continue
            h = q.group(1)
        else:
            m = path_re.search(path)
            if not m:
                continue
            h = m.group(1)
        h = _clean(h)
        if not h or h.lower() in _BAD_HANDLES:
            continue
        return Handle(platform, h, url)
    return None


def from_text(text: str, source_url: str = "") -> list[Handle]:
    """從內文抽 ID：PTT 推文/作者、@提及。回傳去重後結果。"""
    found: dict[tuple[str, str], Handle] = {}
    dom = (urlparse(source_url).hostname or "").lower() if source_url else ""

    if "ptt.cc" in dom or _PTT_PUSH.search(text) or _PTT_AUTHOR.search(text):
        for m in _PTT_AUTHOR.finditer(text):
            h = Handle("ptt", m.group(1), source_url)
            found[h.ident()] = h
        for m in _PTT_PUSH.finditer(text):
            h = Handle("ptt", m.group(1), source_url)
            found.setdefault(h.ident(), h)

    for m in _AT_MENTION.finditer(text):
        raw = m.group(1).rstrip(".")
        if len(raw) < 3 or raw.lower() in _BAD_HANDLES or "." in raw and raw.count(".") > 2:
            continue
        h = Handle("mention", raw, source_url)
        found.setdefault(h.ident(), h)

    return list(found.values())


def extract_all(url: str, text: str) -> list[Handle]:
    out: dict[tuple[str, str], Handle] = {}
    h = from_url(url)
    if h:
        out[h.ident()] = h
    for h2 in from_text(text, url):
        out.setdefault(h2.ident(), h2)
    return list(out.values())


def handle_in_text(handle: str, text: str) -> bool:
    """驗證：這個 handle 是否真的出現在文字裡（不分大小寫）。"""
    return bool(handle) and handle.lower().lstrip("@") in text.lower()


def seed_known_handles(known: list[str]) -> dict[tuple[str, str], Handle]:
    """把使用者提供的已知帳號預先變成 Handle（多平台 canonical URL）。

    這些 ID 保證來自使用者輸入，可安全進入候選池讓 LLM 評分。
    若搜尋結果的 URL/摘要後來對上，會覆蓋成更精確的 platform/url。
    """
    out: dict[tuple[str, str], Handle] = {}
    for raw in known:
        h = _clean(raw.lstrip("@"))
        if not h or len(h) < 2 or h.lower() in _BAD_HANDLES:
            continue
        # 先放一個通用的「known」平台，之後若 URL 對上會被更精確的覆蓋
        out.setdefault(("known", h.lower()), Handle("known", h, ""))
        for plat, tmpl in _PROFILE_TEMPLATES.items():
            url = tmpl.format(h=h)
            key = (plat, h.lower())
            out.setdefault(key, Handle(plat, h, url))
    return out


def extract_known_from_hits(
    known: list[str],
    hits: list,  # list[SearchHit] — 避免循環 import，用 duck typing
    pages: dict | None = None,
) -> dict[tuple[str, str], Handle]:
    """當已知帳號出現在搜尋結果的 URL / 標題 / 摘要 / 頁面文字中時，抽出對應 Handle。

    這比單純 seed 更強：只有「確實出現在來源」的才會被標成有證據的候選。
    """
    out: dict[tuple[str, str], Handle] = {}
    known_set = {_clean(k.lstrip("@")).lower() for k in known if k}
    if not known_set:
        return out

    for hit in hits:
        url = getattr(hit, "url", "") or ""
        title = getattr(hit, "title", "") or ""
        snippet = getattr(hit, "snippet", "") or ""
        blob = f"{title}\n{snippet}"

        # 1) URL 規則優先
        hd = from_url(url)
        if hd and hd.handle.lower() in known_set:
            out[hd.ident()] = hd

        # 2) 標題/摘要裡出現已知 handle（含 @ 或不含）
        for kh in known_set:
            if kh in blob.lower() or f"@{kh}" in blob.lower():
                # 嘗試從 URL 推平台，否則用 known
                if hd and hd.handle.lower() == kh:
                    out[hd.ident()] = hd
                else:
                    # 從 URL host 猜平台
                    host = (urlparse(url).hostname or "").lower()
                    plat = "known"
                    for p, dom_re, _ in _URL_RULES:
                        if dom_re.search(host.replace("www.", "")):
                            plat = p
                            break
                    key = (plat, kh)
                    if key not in out:
                        tmpl = _PROFILE_TEMPLATES.get(plat)
                        purl = tmpl.format(h=kh) if tmpl else url
                        out[key] = Handle(plat, kh, purl or url)

    if pages:
        for url, page in pages.items():
            text = getattr(page, "text", "") or ""
            for kh in known_set:
                if kh in text.lower():
                    hd = from_url(url)
                    if hd and hd.handle.lower() == kh:
                        out[hd.ident()] = hd
                    else:
                        key = ("known", kh)
                        out.setdefault(key, Handle("known", kh, url))

    return out
