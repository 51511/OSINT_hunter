"""端對端測試：假 LLM + 假 SearXNG + 假 Scraper，驗證整條管線與各層防護。"""
import json

from osint_hunter.config import Settings
from osint_hunter.llm import LLM
from osint_hunter.models import Page, SearchHit, Target
from osint_hunter.pipeline import Investigator

PTT_URL = "https://www.ptt.cc/bbs/Design/M.111.A.html"
IG_URL = "https://www.instagram.com/xiaoming.design/"
BLOG_URL = "https://xm-blog.pixnet.net/blog/post/9"
OTHER_URL = "https://www.ptt.cc/bbs/Gossiping/M.222.A.html"

PTT_TEXT = "作者 xmdesign (小明)\n我住在台北，做平面設計工作十年了\n推 fan001: 小明超強\n推 stranger9: 路過"
OTHER_TEXT = "作者 kaohsiung1 (小明)\n我在高雄當工程師\n推 zzz: 哈哈"


class FakeSearx:
    def __init__(self): self.calls = []
    def health(self): return {"ok": True, "latency_ms": 1, "error": "", "json_enabled": True}
    def batch(self, queries, seen=None, on_progress=None):
        seen = seen if seen is not None else set()
        self.calls.append(list(queries))
        pool = [
            SearchHit("小明 設計師", IG_URL, "小明 | 台北平面設計師", "google", queries[0]),
            SearchHit("Re: 設計", PTT_URL, "我住在台北 平面設計", "google", queries[0]),
            SearchHit("小明的部落格", BLOG_URL, "台北設計日記", "bing", queries[0]),
            SearchHit("高雄小明", OTHER_URL, "我在高雄當工程師", "bing", queries[0]),
        ]
        fresh = []
        for h in pool:
            if h.key() in seen: continue
            seen.add(h.key()); fresh.append(h)
        return fresh, len(pool)


class FakeScraper:
    def scrape_many(self, hits):
        texts = {PTT_URL: PTT_TEXT, OTHER_URL: OTHER_TEXT,
                 BLOG_URL: "小明的設計日記 台北 平面設計 聯絡 @xmdesign"}
        out = {}
        for h in hits:
            if h.url == IG_URL:
                out[h.url] = Page(h.url, h.title, f"{h.title}\n{h.snippet}", False, "snippet-only")
            else:
                out[h.url] = Page(h.url, h.title, texts.get(h.url, h.snippet), True)
        return out


class ScriptedLLM(LLM):
    """依 system prompt 內容回傳預先寫好的 JSON，並可注入「幻覺」。"""
    def __init__(self, settings, hallucinate=True):
        super().__init__(settings, "anthropic", "fake")
        self.hallucinate = hallucinate
        self.seen = []
    def complete_json(self, system, user, **kw):
        self.seen.append(system[:20])
        if "產生剛好" in system:
            return {"queries": ["小明 台北 設計 site:ptt.cc", "小明 設計師 site:instagram.com", "xmdesign"]}
        if "搜尋結果清單" in system:
            return {"keep": [1, 2, 3, 4]}
        if "程式已從網址" in system or "候選 ID" in system:
            cands = [
                {"platform": "ptt", "handle": "xmdesign", "profile_url": "", "confidence": "high",
                 "reasoning": "台北+設計吻合", "conflicts": "",
                 "evidence": [{"url": PTT_URL, "quote": "我住在台北，做平面設計工作十年了"},
                              {"url": PTT_URL, "quote": "作者 xmdesign (小明)"}]},
                {"platform": "instagram", "handle": "xiaoming.design", "confidence": "high",
                 "reasoning": "摘要吻合", "conflicts": "",
                 "evidence": [{"url": IG_URL, "quote": "小明 | 台北平面設計師"},
                              {"url": IG_URL, "quote": "小明 | 台北"}]},
                {"platform": "ptt", "handle": "kaohsiung1", "confidence": "high",
                 "reasoning": "同名", "conflicts": "",
                 "evidence": [{"url": OTHER_URL, "quote": "我在高雄當工程師"}]},
            ]
            if self.hallucinate:
                cands.append({"platform": "instagram", "handle": "totally_fake_id", "confidence": "high",
                              "reasoning": "杜撰", "evidence": [{"url": PTT_URL, "quote": "他其實在紐約開公司"}]})
            return {"candidates": cands,
                    "new_leads": ["fan001", "杜撰暱稱XYZ"] if self.hallucinate else ["fan001"]}
        return {}
    def complete(self, system, user, **kw):
        return "## 調查目標\n（報告）"


def make(hallucinate=True, rounds=2):
    s = Settings(); s.max_rounds = rounds; s.search_qps_delay = 0
    s.queries_per_round = 3; s.max_pages_to_scrape = 10
    llm = ScriptedLLM(s, hallucinate)
    inv = Investigator(s, llm, searx=FakeSearx(), scraper=FakeScraper())
    return inv, llm


def test_full_run_and_guards():
    inv, llm = make(hallucinate=True, rounds=2)
    res = inv.run(Target("小明", "台北 平面設計"))
    by = {c.handle: c for c in res.candidates}

    # 防護 1：LLM 自行新增、不在程式抽出清單的 ID 必須被丟棄
    assert "totally_fake_id" not in by

    # 防護 2：真證據 + 完整頁面 → 維持 high
    assert by["xmdesign"].confidence == "high"
    assert all(e.verified for e in by["xmdesign"].evidence)

    # 防護 3：IG 只有 snippet → 最高 medium
    assert by["xiaoming.design"].confidence == "medium"
    assert "搜尋摘要" in by["xiaoming.design"].conflicts

    # 防護 4：只有 1 則證據卻標 high → 降級
    assert by["kaohsiung1"].confidence == "medium"

    # 防護 5：杜撰的線索被過濾，真實的保留
    all_leads = [l for r in res.rounds for l in r.new_handles]
    assert "fan001" in all_leads
    assert "杜撰暱稱XYZ" not in all_leads

    # 第二輪確實用了線索
    assert len(res.rounds) >= 1
    assert res.report_md.startswith("##")


def test_dedup_across_rounds():
    inv, _ = make(hallucinate=False, rounds=3)
    res = inv.run(Target("小明", "台北"))
    keys = [c.ident() for c in res.candidates]
    assert len(keys) == len(set(keys)), "跨輪候選必須去重"


def test_searx_down_graceful():
    inv, _ = make()
    inv.searx.health = lambda: {"ok": False, "error": "connection refused", "latency_ms": 0, "json_enabled": False}
    res = inv.run(Target("小明"))
    assert res.warnings and "SearXNG 不可用" in res.warnings[0]
    assert res.candidates == []
