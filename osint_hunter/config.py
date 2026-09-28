"""集中設定：全部從環境變數 / .env 讀取。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return default
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass
class Settings:
    # --- LLM ---
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_base_url: str = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL", ""))
    ollama_base_url: str = field(
        default_factory=lambda: os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    )
    default_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "anthropic"))
    default_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "claude-sonnet-4-6"))
    llm_max_tokens: int = field(default_factory=lambda: _int("LLM_MAX_TOKENS", 4096))
    llm_temperature: float = field(default_factory=lambda: _float("LLM_TEMPERATURE", 0.2))

    # --- SearXNG ---
    searxng_url: str = field(default_factory=lambda: os.getenv("SEARXNG_URL", "http://localhost:8080"))
    searxng_engines: list[str] = field(
        default_factory=lambda: _list("SEARXNG_ENGINES", ["google", "bing", "duckduckgo", "brave"])
    )
    searxng_language: str = field(default_factory=lambda: os.getenv("SEARXNG_LANGUAGE", "zh-TW"))
    searxng_timeout: int = field(default_factory=lambda: _int("SEARXNG_TIMEOUT", 30))
    search_qps_delay: float = field(default_factory=lambda: _float("SEARCH_DELAY", 0.8))

    # --- 管線 ---
    max_rounds: int = field(default_factory=lambda: _int("MAX_ROUNDS", 3))
    queries_per_round: int = field(default_factory=lambda: _int("QUERIES_PER_ROUND", 10))
    max_results_per_query: int = field(default_factory=lambda: _int("MAX_RESULTS_PER_QUERY", 15))
    max_results_to_filter: int = field(default_factory=lambda: _int("MAX_RESULTS_TO_FILTER", 60))
    max_pages_to_scrape: int = field(default_factory=lambda: _int("MAX_PAGES_TO_SCRAPE", 12))
    scrape_threads: int = field(default_factory=lambda: _int("SCRAPE_THREADS", 6))
    scrape_max_chars: int = field(default_factory=lambda: _int("SCRAPE_MAX_CHARS", 6000))
    scrape_timeout: int = field(default_factory=lambda: _int("SCRAPE_TIMEOUT", 20))
    max_new_handles_per_round: int = field(default_factory=lambda: _int("MAX_NEW_HANDLES", 4))

    # --- 儲存 ---
    investigations_dir: Path = field(
        default_factory=lambda: Path(os.getenv("INVESTIGATIONS_DIR", "investigations"))
    )

    # --- 抓取政策 ---
    # 這些網站需登入或有反爬，抓取無意義：只使用搜尋引擎給的 snippet。
    snippet_only_domains: list[str] = field(
        default_factory=lambda: _list(
            "SNIPPET_ONLY_DOMAINS",
            [
                "instagram.com", "threads.net", "threads.com", "facebook.com",
                "fb.com", "twitter.com", "x.com", "tiktok.com", "linkedin.com",
            ],
        )
    )
    respect_robots: bool = field(default_factory=lambda: os.getenv("RESPECT_ROBOTS", "1") == "1")


def get_settings() -> Settings:
    return Settings()
