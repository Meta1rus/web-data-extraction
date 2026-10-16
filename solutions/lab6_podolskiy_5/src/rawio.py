"""Сырой слой: только дозапись JSON Lines + контроль неизменяемости.

Правила:
* файл открывается только в режиме "a" (дозапись), существующие строки не трогаются;
* после каждой дозаписи в raw/MANIFEST.jsonl добавляется строка с размером файла
  и SHA-256 его содержимого;
* перед каждой дозаписью и перед пересборкой clean проверяется, что первые
  size_after байт файла по-прежнему дают тот же SHA-256. Если кто-то отредактировал
  или перезаписал сырой файл, проверка падает с RawTamperedError;
* между дозаписями файл получает права только на чтение (0444) — защита от
  случайной правки в редакторе.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from . import config


class RawTamperedError(RuntimeError):
    """Содержимое сырого файла не совпадает с зафиксированным в манифесте."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _sha256_prefix(path: Path, size: int) -> str:
    h = hashlib.sha256()
    remaining = size
    with path.open("rb") as f:
        while remaining > 0:
            chunk = f.read(min(1 << 20, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def _manifest_entries(manifest: Path) -> list[dict]:
    if not manifest.exists():
        return []
    with manifest.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def verify(path: Path, manifest: Path | None = None) -> dict:
    """Проверяет, что файл — дозаписанное продолжение зафиксированного состояния."""
    manifest = manifest or config.RAW_MANIFEST
    entries = [e for e in _manifest_entries(manifest) if e["file"] == path.name]
    if not entries:
        return {"file": path.name, "checked": False, "reason": "нет записей в манифесте"}
    last = entries[-1]
    if not path.exists():
        raise RawTamperedError(f"{path.name}: файл удалён, а в манифесте {len(entries)} дозаписей")
    size = path.stat().st_size
    if size < last["size_after"]:
        raise RawTamperedError(f"{path.name}: файл стал короче ({size} < {last['size_after']} байт)")
    if _sha256_prefix(path, last["size_after"]) != last["sha256_after"]:
        raise RawTamperedError(f"{path.name}: изменены уже записанные строки (SHA-256 префикса не совпал)")
    return {"file": path.name, "checked": True, "appends": len(entries),
            "size_fixed": last["size_after"], "size_now": size}


def verify_all(raw_dir: Path | None = None) -> list[dict]:
    raw_dir = raw_dir or config.RAW_DIR
    return [verify(p) for p in sorted(raw_dir.glob("*.jsonl")) if p.name != config.RAW_MANIFEST.name]


def append_records(path: Path, records: Iterable[dict], run_id: str) -> dict:
    """Дописывает записи в конец файла и фиксирует новое состояние в манифесте."""
    path.parent.mkdir(parents=True, exist_ok=True)
    verify(path)                                   # сначала убеждаемся, что старое не тронуто
    size_before = path.stat().st_size if path.exists() else 0
    if path.exists():
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    n = 0
    with path.open("a", encoding="utf-8") as f:    # только дозапись
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    size_after = path.stat().st_size
    entry = {"file": path.name, "run_id": run_id, "appended_at": utc_now_iso(),
             "lines_added": n, "size_before": size_before, "size_after": size_after,
             "sha256_after": _sha256_prefix(path, size_after)}
    with config.RAW_MANIFEST.open("a", encoding="utf-8") as m:
        m.write(json.dumps(entry, ensure_ascii=False) + "\n")
    os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)   # 0444 до следующей дозаписи
    return entry


def iter_jsonl(path: Path) -> Iterator[tuple[int, dict | None, str]]:
    """Построчное чтение: (номер строки, запись или None, исходная строка).

    Повреждённая строка не роняет чтение остальных — она возвращается с None
    и дальше попадает в отбраковку с причиной «нечитаемая строка JSON».
    """
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                yield i, json.loads(line), line
            except json.JSONDecodeError:
                yield i, None, line


def read_jsonl(path: Path) -> list[dict]:
    return [rec for _, rec, _ in iter_jsonl(path) if rec is not None]
