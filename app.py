"""Streamlit 介面：  streamlit run app.py"""
from __future__ import annotations

import json
from datetime import datetime

import streamlit as st

from osint_hunter.config import get_settings
from osint_hunter.llm import LLM, LLMError
from osint_hunter.models import Investigation, Target
from osint_hunter.pipeline import Investigator
from osint_hunter.search import SearXNG
from osint_hunter.store import list_saved, load, save

st.set_page_config(page_title="OSINT Hunter", page_icon="🕵️", layout="wide")
S = get_settings()

CONF_STYLE = {"high": ("🟢", "高"), "medium": ("🟡", "中"), "low": ("🔴", "低")}

# ───────────────────────── Sidebar ─────────────────────────
with st.sidebar:
    st.title("🕵️ OSINT Hunter")
    st.caption("LLM 驅動的明網 OSINT 代理")

    st.subheader("LLM")
    provider = st.selectbox("Provider", ["anthropic", "openai", "ollama", "custom"],
                            index=["anthropic", "openai", "ollama", "custom"].index(S.default_provider)
                            if S.default_provider in ("anthropic", "openai", "ollama", "custom") else 0)
    model = st.text_input("Model", value=S.default_model)

    st.subheader("調查參數")
    rounds = st.slider("最大輪數", 1, 6, S.max_rounds, help="每輪會用上一輪發現的新線索反查")
    S.queries_per_round = st.slider("每輪查詢數", 4, 20, S.queries_per_round)
    S.max_pages_to_scrape = st.slider("每輪最多抓取頁數", 3, 30, S.max_pages_to_scrape)
    S.scrape_threads = st.slider("抓取執行緒", 1, 12, S.scrape_threads)
    S.respect_robots = st.checkbox("遵守 robots.txt", value=S.respect_robots)

    st.subheader("SearXNG")
    S.searxng_url = st.text_input("URL", value=S.searxng_url)
    engines = st.text_input("引擎（逗號分隔）", value=",".join(S.searxng_engines))
    S.searxng_engines = [e.strip() for e in engines.split(",") if e.strip()]

    if st.button("🔌 連線檢查", use_container_width=True):
        h = SearXNG(S).health()
        (st.success if h["ok"] else st.error)(
            f"SearXNG {'OK' if h['ok'] else 'FAIL'} ({h['latency_ms']}ms) {h['error']}")
        try:
            LLM(S, provider, model).complete("測試", "回覆：好", max_tokens=16)
            st.success(f"LLM OK：{provider}:{model}")
        except Exception as e:
            st.error(f"LLM FAIL：{e}")

    st.divider()
    st.subheader("📂 過去調查")
    saved = list_saved(S.investigations_dir)
    if saved:
        pick = st.selectbox("載入", ["（無）"] + [p.name for p in saved], key="pick")
        if pick != "（無）" and st.button("載入", use_container_width=True):
            st.session_state["inv"] = load(S.investigations_dir / pick)
            st.rerun()
    else:
        st.caption("尚無紀錄")

# ───────────────────────── 輸入 ─────────────────────────
st.header("目標")
with st.form("target_form"):
    c1, c2 = st.columns([1, 2])
    name = c1.text_input("目標名稱 *", placeholder="小明")
    anchors = c2.text_input("錨點資訊", placeholder="台北、平面設計師、台大…（越多越能避免同名誤判）")
    known = st.text_input("已知帳號/暱稱（逗號分隔）", placeholder="xmdesign, ming_d")
    st.caption("⚠️ 請僅在有正當目的（如驗證詐騙帳號、失聯聯絡、交易對象查證）時使用，並遵守個資法規。")
    go = st.form_submit_button("🔍 開始調查", type="primary")

# ───────────────────────── 執行 ─────────────────────────
if go:
    if not name.strip():
        st.error("請輸入目標名稱")
        st.stop()

    target = Target(name.strip(), anchors.strip(), [x.strip() for x in known.split(",") if x.strip()])
    llm = LLM(S, provider, model)

    log_box = st.status("調查進行中…", expanded=True)
    live = log_box.empty()
    lines: list[str] = []

    def progress(stage: str, msg: str) -> None:
        if stage == "queries":
            msg = "查詢：" + " ｜ ".join(json.loads(msg))
        icon = {"round": "━", "expand": "🧠", "search": "🔍", "filter": "🗂️", "scrape": "📜",
                "score": "⚖️", "verify": "✅", "info": "ℹ️", "error": "❌", "queries": "📝"}.get(stage, "·")
        if stage == "search":
            live.markdown("\n\n".join(lines[-12:] + [f"{icon} {msg}"]))
            return
        lines.append(f"{icon} {msg}")
        live.markdown("\n\n".join(lines[-12:]))

    try:
        inv = Investigator(S, llm, progress=progress).run(target, rounds)
        save(inv, S.investigations_dir)
        st.session_state["inv"] = inv
        log_box.update(label="調查完成", state="complete", expanded=False)
    except LLMError as e:
        log_box.update(label="失敗", state="error")
        st.error(str(e))

# ───────────────────────── 顯示結果 ─────────────────────────
inv: Investigation | None = st.session_state.get("inv")
if inv:
    st.divider()
    st.subheader(f"結果：{inv.target.name}")
    for w in inv.warnings:
        st.warning(w)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("輪數", len(inv.rounds))
    m2.metric("搜尋結果", len(inv.hits))
    m3.metric("完整抓取頁面", sum(1 for p in inv.pages.values() if p.fetched))
    m4.metric("候選帳號", len(inv.candidates))

    tab_r, tab_c, tab_s, tab_l = st.tabs(["📄 報告", "👤 候選帳號", "🔗 來源", "🧾 過程"])

    with tab_r:
        st.markdown(inv.report_md or "_無報告_")
        if inv.report_md:
            st.download_button("下載報告 (.md)", inv.report_md,
                               file_name=f"report_{datetime.now():%Y%m%d_%H%M}.md")

    with tab_c:
        rank = {"high": 0, "medium": 1, "low": 2}
        if not inv.candidates:
            st.info("沒有找到候選帳號。可嘗試補充錨點資訊，或增加輪數。")
        for c in sorted(inv.candidates, key=lambda x: rank[x.confidence]):
            icon, zh = CONF_STYLE[c.confidence]
            ok = sum(1 for e in c.evidence if e.verified)
            with st.expander(f"{icon} {c.platform} / {c.handle}　信心：{zh}　證據 {ok}/{len(c.evidence)} 已驗證"):
                if c.profile_url:
                    st.markdown(f"[{c.profile_url}]({c.profile_url})")
                st.markdown(f"**判斷理由**：{c.reasoning or '—'}")
                if c.conflicts:
                    st.markdown(f"**⚠️ 疑慮**：{c.conflicts}")
                for e in c.evidence:
                    tag = "✅ 已驗證" if e.verified else "❌ 未能在來源比對到（不可採信）"
                    st.markdown(f"> {e.quote}\n\n{tag}　[{e.url}]({e.url})")

    with tab_s:
        for p in inv.pages.values():
            tag = "📄 完整" if p.fetched else f"🔸 摘要（{p.note}）"
            st.markdown(f"- {tag}　[{p.title or p.url}]({p.url})")

    with tab_l:
        for r in inv.rounds:
            st.markdown(f"**第 {r.round_no} 輪**　命中 {r.hits}　新增 {r.new_hits}　"
                        f"篩選 {r.filtered}　抓取 {r.scraped}")
            st.code("\n".join(r.queries), language="text")
            if r.new_handles:
                st.markdown("新線索：" + "、".join(f"`{x}`" for x in r.new_handles))
