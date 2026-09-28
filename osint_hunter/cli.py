"""命令列介面：  python -m osint_hunter.cli "小明" -a "台北 設計師" """
from __future__ import annotations

import argparse
import logging
import sys

from .config import get_settings
from .llm import LLM, LLMError
from .models import Target
from .pipeline import Investigator
from .search import SearXNG
from .store import save


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="osint-hunter", description="LLM 驅動的明網 OSINT 代理")
    ap.add_argument("name", nargs="?", help="目標名稱")
    ap.add_argument("-a", "--anchors", default="", help="錨點資訊：地區、職業、學校…")
    ap.add_argument("-k", "--known", default="", help="已知帳號（handle），逗號分隔")
    ap.add_argument("-n", "--nicknames", default="", help="暱稱/別名，逗號分隔（如：阿丹,小瑜）")
    ap.add_argument("-r", "--rounds", type=int, help="最大輪數")
    ap.add_argument("--provider", help="anthropic | openai | ollama | custom")
    ap.add_argument("-m", "--model", help="模型名稱")
    ap.add_argument("--check", action="store_true", help="只做連線檢查（SearXNG + LLM）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    s = get_settings()
    llm = LLM(s, args.provider, args.model)

    if args.check:
        h = SearXNG(s).health()
        print(f"SearXNG  {'OK ' if h['ok'] else 'FAIL'} {h['latency_ms']}ms {h['error']}")
        try:
            out = llm.complete("你是測試。", "回覆一個字：好", max_tokens=16)
            print(f"LLM      OK  {llm.provider}:{llm.model} → {out.strip()[:20]!r}")
        except LLMError as e:
            print(f"LLM      FAIL {e}")
            return 1
        return 0 if h["ok"] else 1

    if not args.name:
        ap.error("需要提供目標名稱（或使用 --check）")

    known = [x.strip() for x in args.known.split(",") if x.strip()]
    nicks = [x.strip() for x in args.nicknames.split(",") if x.strip()]
    target = Target(args.name, args.anchors, known, nicks)

    def prog(stage: str, msg: str) -> None:
        icons = {"round": "━━", "expand": "🧠", "queries": "📝", "search": "🔍", "filter": "🗂️ ",
                 "scrape": "📜", "score": "⚖️ ", "verify": "✅", "info": "ℹ️ ", "error": "❌"}
        if stage == "queries":
            import json
            for q in json.loads(msg):
                print(f"     · {q}")
        elif stage == "search":
            # 用空格清掉上一行殘字，避免「命中 33，新增 33.com …」這類顯示污染
            print(f"\r  {icons.get(stage, '')} {msg:<80}", end="", flush=True)
        else:
            if stage != "round":
                print()  # 結束 search 的 \r 行
            print(f"{icons.get(stage,'')} {msg}" if stage == "round" else f"  {icons.get(stage,'')} {msg}")

    try:
        inv = Investigator(s, llm, progress=prog).run(target, args.rounds)
    except LLMError as e:
        print(f"\n❌ {e}", file=sys.stderr)
        return 1

    path = save(inv, s.investigations_dir)
    print("\n" + "═" * 60 + "\n" + inv.report_md + "\n" + "═" * 60)
    for w in inv.warnings:
        print(f"⚠️  {w}")
    print(f"\n已儲存：{path}（同名 .md 為報告）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
