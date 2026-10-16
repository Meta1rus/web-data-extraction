"""Перепарсинг сохранённых HTML-страниц без обращения к сайту.

Сценарий: GitHub поменял разметку, часть *_raw-полей пришла пустой. Исправляем
селекторы в src/parsers.py, поднимаем HTML_PARSER_VERSION в src/config.py и запускаем

    python -m src.reparse_html            # только страницы, разобранные старой версией парсера
    python -m src.reparse_html --force    # все сохранённые страницы

Скрипт читает raw/html/<run_id>/*.html.gz (их сохраняет src.collect), разбирает
заново и ДОПИСЫВАЕТ новые записи в raw/github_html.jsonl (старые строки не трогаются).
У новых записей прежние _source_url/_collected_at/_status, новая _parser_version и
отметка _reparsed_at; при очистке они побеждают старые как более поздние строки
с тем же временем сбора. Затем — python -m src.rebuild.
"""
from __future__ import annotations

import argparse
import gzip
from collections import defaultdict

from . import config, parsers, rawio

META_KEEP = ("_entity", "_source", "_source_url", "_collected_at", "_status", "_run_id", "_final_url",
             "_html_file", "repo_slug_requested")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Перепарсинг raw/html без сети")
    ap.add_argument("--force", action="store_true", help="перепарсить и страницы текущей версии")
    args = ap.parse_args(argv)
    rawio.verify_all()
    records = rawio.read_jsonl(config.RAW_HTML)
    by_file: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        if r.get("_html_file") and "_error" not in r:
            by_file[r["_html_file"]].append(r)
    done = {r["_html_file"] for r in records
            if r.get("_parser_version") == config.HTML_PARSER_VERSION and r.get("_reparsed_at")}
    new: list[dict] = []
    stamp = rawio.utc_now_iso()
    for rel_path, recs in sorted(by_file.items()):
        latest_version = recs[-1].get("_parser_version")
        if not args.force and (latest_version == config.HTML_PARSER_VERSION or rel_path in done):
            continue
        path = config.RAW_DIR / rel_path
        if not path.exists():
            print(f"  нет файла {rel_path} — пропуск")
            continue
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            html = fh.read()
        base = {k: recs[0].get(k) for k in META_KEEP}
        extra = {"_parser_version": config.HTML_PARSER_VERSION, "_reparsed_from": latest_version,
                 "_reparsed_at": stamp}
        if base["_entity"] == "repo_page":
            new.append({**parsers.parse_repo_page(html), **base, **extra})
        else:
            rels, _ = parsers.parse_releases_page(html)
            new.extend({**rel, **base, **extra} for rel in rels)
    if not new:
        print("Перепарсить нечего: все страницы разобраны текущей версией "
              f"{config.HTML_PARSER_VERSION} (используйте --force).")
        return 0
    entry = rawio.append_records(config.RAW_HTML, new, run_id=f"reparse-{stamp}")
    print(f"Дописано {entry['lines_added']} записей версии {config.HTML_PARSER_VERSION}; "
          f"дальше: python -m src.rebuild")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
