"""Общая конфигурация: пути проекта, чтение config.yaml и .env, служебные функции времени и файлов.

Секреты (GITHUB_TOKEN) читаются только из окружения или файла .env и никогда не печатаются целиком.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parents[1]
# Каталог данных (raw/clean/mart/db/logs/reports/state). По умолчанию — корень проекта; переменная
# PIPELINE_DATA_ROOT позволяет прогнать имитацию в отдельный каталог, не смешивая её с реальными данными.
DATA_ROOT = Path(os.getenv("PIPELINE_DATA_ROOT") or ROOT).resolve()
DIRS = {name: DATA_ROOT / name for name in ("raw", "clean", "mart", "db", "logs", "reports", "state")}
DIRS.update({name: ROOT / name for name in ("nocode", "docs")})
INCIDENTS_DIR = DIRS["logs"] / "incidents"
HTML_SNAPSHOTS_DIR = DIRS["raw"] / "html_pages"
CACHE_DIR = DATA_ROOT / ".cache" / "http"

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def ensure_dirs() -> None:
    for d in (*DIRS.values(), INCIDENTS_DIR, HTML_SNAPSHOTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load_dotenv(path: Path | None = None) -> None:
    """Минимальный разбор .env (KEY=VALUE). Уже заданные переменные окружения не перезаписываются."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Читает config.yaml и применяет переопределения из окружения."""
    load_dotenv()
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    html_base = os.getenv("GITHUB_HTML_BASE")
    api_base = os.getenv("GITHUB_API_BASE")
    if html_base:
        cfg["sources"]["html"]["base_url"] = html_base.rstrip("/")
    if api_base:
        cfg["sources"]["api"]["base_url"] = api_base.rstrip("/")
    cfg["_config_path"] = str(cfg_path)
    return cfg


def github_token() -> str | None:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    return token or None


def mask_secret(secret: str | None) -> str:
    if not secret:
        return "нет"
    if len(secret) <= 8:
        return "***"
    return f"{secret[:4]}…{secret[-2:]} (длина {len(secret)})"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    """Единый формат времени во всём проекте: ISO 8601, UTC, секунды, суффикс Z."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("наивная дата без часового пояса недопустима")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def host_of(url: str) -> str:
    return urlparse(url).netloc


def is_local_url(url: str) -> bool:
    return (urlparse(url).hostname or "") in LOCAL_HOSTS


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else default


def write_json_atomic(path: Path, data: Any) -> None:
    """Запись через временный файл и os.replace: прерванный прогон не оставит «полуфайл» состояния."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)
