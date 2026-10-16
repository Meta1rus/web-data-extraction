"""Общие настройки: пути слоёв, адреса источников, версии парсеров.

Все пути строятся от DATA_ROOT. По умолчанию это корень лабораторной; для тестов
на фикстурах его подменяют переменной окружения LAB6_DATA_ROOT, чтобы тестовые
данные не смешивались с реальными.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.getenv("LAB6_DATA_ROOT", str(PROJECT_ROOT))).resolve()

RAW_DIR = DATA_ROOT / "raw"
CLEAN_DIR = DATA_ROOT / "clean"
MART_DIR = DATA_ROOT / "mart"
DB_DIR = DATA_ROOT / "db"
LOG_DIR = DATA_ROOT / "logs"

# Сырой слой: один файл на источник (+ отдельный файл синтетической «порчи»)
RAW_API = RAW_DIR / "github_api.jsonl"
RAW_HTML = RAW_DIR / "github_html.jsonl"
RAW_SYNTHETIC = RAW_DIR / "synthetic_dirty.jsonl"
RAW_MANIFEST = RAW_DIR / "MANIFEST.jsonl"
RAW_HTML_PAGES = RAW_DIR / "html"

CLEAN_EVENTS = CLEAN_DIR / "dataset.parquet"          # основной чистый набор: события
CLEAN_REPOS = CLEAN_DIR / "repositories.parquet"      # справочник репозиториев (1 → N событий)
CLEAN_REJECTS = CLEAN_DIR / "rejects.jsonl"
CLEAN_LOG = CLEAN_DIR / "cleaning_log.csv"
FORMAT_BENCH = CLEAN_DIR / "format_benchmark.csv"

DB_SCHEMA = PROJECT_ROOT / "db" / "schema.sql"        # схема — часть кода, а не данных
WAREHOUSE_DB = DB_DIR / "warehouse.db"
DEMO_DB = DB_DIR / "demo_idempotency.db"

# Источники. Адреса можно подменить только для офлайн-тестов на локальной имитации.
GITHUB_API_URL = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
GITHUB_WEB_URL = os.getenv("GITHUB_WEB_URL", "https://github.com").rstrip("/")
CANONICAL_WEB = "https://github.com"   # канонический адрес для бизнес-ключа

USER_AGENT = "PSU-edu-crawler/1.0 (+https://github.com/Meta1rus/web-data-extraction; student lab)"

API_PARSER_VERSION = "github-api/1.0"
HTML_PARSER_VERSION = "github-html/1.1"  # 1.1: встроенный JSON React-страницы
CLEAN_VERSION = "clean/1.0"

SOURCE_API = "github_api"
SOURCE_HTML = "github_html"


def ensure_dirs() -> None:
    for d in (RAW_DIR, CLEAN_DIR, MART_DIR / "views", MART_DIR / "snapshots",
              MART_DIR / "charts", DB_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def mask(secret: str | None) -> str:
    """Секрет никогда не печатается целиком."""
    if not secret:
        return "<нет>"
    if len(secret) <= 8:
        return "***"
    return secret[:4] + "…" + secret[-2:]
