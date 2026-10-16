"""Пересборка чистого слоя из raw без обращения к сайту.

    python -m src.rebuild            # проверить raw → пересобрать clean/
    python -m src.rebuild --verify   # то же + доказать детерминированность: отпечаток clean
                                     # до удаления и после пересборки должен совпасть

Почему это возможно: в raw лежат все целевые поля «как пришли» (для HTML — строки
*_raw вроде «54.3k»), метаданные происхождения и (опционально) сами HTML-страницы
в raw/html/. Вся логика разбора значений — в src/clean.py, поэтому исправление
парсера чисел/дат применяется к уже собранным данным одной командой.
На время пересборки сеть программно запрещается (socket.connect → ошибка).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import time
from contextlib import contextmanager

import pandas as pd

from . import clean, config, rawio


class NetworkForbidden(RuntimeError):
    pass


@contextmanager
def no_network():
    """Любая попытка открыть сетевое соединение во время пересборки — исключение."""
    original_connect = socket.socket.connect
    original_create = socket.create_connection

    def deny(*_a, **_k):
        raise NetworkForbidden("пересборка clean не должна обращаться к сети")

    socket.socket.connect = deny
    socket.create_connection = deny
    try:
        yield
    finally:
        socket.socket.connect = original_connect
        socket.create_connection = original_create


def fingerprint() -> dict:
    """Отпечаток содержимого чистого слоя (не байтов файла): строки сортируются по ключу."""
    out = {}
    for name, path, key in (("events", config.CLEAN_EVENTS, "event_key"),
                            ("repositories", config.CLEAN_REPOS, "repo_key")):
        if not path.exists():
            out[name] = None
            continue
        df = pd.read_parquet(path).sort_values(key).reset_index(drop=True)
        text = df.astype(str).to_csv(index=False)
        out[name] = {"rows": len(df), "sha256": hashlib.sha256(text.encode()).hexdigest()[:16]}
    if config.CLEAN_REJECTS.exists():
        data = config.CLEAN_REJECTS.read_bytes()
        out["rejects"] = {"rows": data.count(b"\n"), "sha256": hashlib.sha256(data).hexdigest()[:16]}
    return out


def rebuild(verify: bool = False) -> dict:
    checks = rawio.verify_all()
    before = fingerprint() if verify else None
    if verify:
        for p in (config.CLEAN_EVENTS, config.CLEAN_REPOS, config.CLEAN_REJECTS, config.CLEAN_LOG):
            p.unlink(missing_ok=True)
    t0 = time.perf_counter()
    with no_network():
        res = clean.build()
        summary = clean.save(res)
    seconds = round(time.perf_counter() - t0, 2)
    after = fingerprint()
    out = {"raw_checks": checks, "seconds": seconds, "clean": summary["clean"], "rejects": summary["rejects"],
           "fingerprint_before": before, "fingerprint_after": after,
           "identical": (before == after) if verify else None}
    (config.LOG_DIR / "rebuild.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Пересборка clean из raw (офлайн)")
    ap.add_argument("--verify", action="store_true", help="сравнить отпечатки до и после")
    args = ap.parse_args(argv)
    out = rebuild(args.verify)
    for c in out["raw_checks"]:
        print(f"raw/{c['file']}: дозаписей {c.get('appends', '—')}, неизменность подтверждена: {c['checked']}")
    print(f"Пересборка без сети: {out['seconds']} с; событий {out['clean']['events']}, "
          f"репозиториев {out['clean']['repositories']}, rejects {out['rejects']}")
    if args.verify:
        print("Отпечаток до :", out["fingerprint_before"])
        print("Отпечаток после:", out["fingerprint_after"])
        print("Результат идентичен:", out["identical"])
        return 0 if out["identical"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
