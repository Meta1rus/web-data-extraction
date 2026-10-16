"""Сводка измерений для отчёта: metrics/summary.md (таблицы, готовые для вставки в report.md).

Источники: metrics/*.json, metrics/bench_*.csv, collection_log.csv, data_*.csv, nocode/nocode_result.csv.
Где значения нет (no-code меряется вручную в n8n) — печатается маркер ⟪ЗАПОЛНИТЬ⟫.
Функции возвращают DataFrame и используются блокнотом lab4.ipynb.
"""
from __future__ import annotations

import json
import sys

import pandas as pd

import common as C

FILL = "⟪ЗАПОЛНИТЬ⟫"


def jload(name: str) -> dict:
    p = C.METRICS / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def fmt(v, nd=2):
    if v is None or v == "":
        return FILL
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def diagnostics_table() -> pd.DataFrame:
    d = jload("diagnostics.json")
    if not d:
        return pd.DataFrame()
    a, b, f = d["anon_page1"], d["auth_page1"], d["login_form"]
    net, nojs, api = d["network"], d["js_disabled"], d.get("api_quotes_anon", {})
    rows = [
        ("Ответ сервера (requests) — /page/1 без входа",
         f"HTTP {a['status']}, {a['bytes']} байт HTML, карточек цитат: {a['quote_cards']}, ссылок «(Goodreads page)»: {a['goodreads_links']}"),
        ("Ответ сервера (requests) — /page/1 после входа",
         f"{b['bytes']} байт, карточек: {b['quote_cards']}, «(Goodreads page)»: {b['goodreads_links']}, ссылка Logout: {b['logout_link']}"),
        ("Поиск целевого текста в исходном HTML",
         f"найден в `{a['first_quote_found_in']}`; `<script>`: {a['script_tags']}, JSON-LD: {a['json_ld']}, "
         f"`__NEXT_DATA__`: {a['next_data']}, `var data`: {a['var_data_json']}"),
        ("Форма /login (requests)",
         f"`{f['method']} {f['action']}`, поля: {', '.join(str(i['name'] or i['type']) for i in f['inputs'])}; "
         f"hidden `csrf_token` {f['csrf_token_len']} симв.; cookie: {', '.join(f['set_cookie_names']) or '—'}; "
         f"метки: {', '.join(l['text'] + '→' + str(l['for']) for l in f['labels'])}"),
        ("POST /login без csrf_token / с csrf_token",
         f"{d['post_without_csrf']['status_chain']} → вход: {d['post_without_csrf']['logged_in']} / "
         f"{d['post_with_csrf']['status_chain']} → вход: {d['post_with_csrf']['logged_in']}"),
        ("DevTools → Network → Fetch/XHR (Playwright, вход + 2 страницы)",
         f"ответов: {net['total']}, по типам: {net['by_type']}; XHR/fetch: {len(net['xhr_fetch'])}; "
         f"сторонние хосты: {', '.join(net['other_hosts']) or 'нет'}"),
        ("XHR-эндпоинт соседнего раздела `/api/quotes?page=1` без входа",
         (f"HTTP {api.get('status')}, {api.get('content_type')}, ключи {api.get('keys')}, "
          f"`goodreads_link` в JSON: {api.get('goodreads_link_present')}") if "status" in api else str(api)),
        ("Работа сайта с отключённым JS",
         f"вход: {nojs['login_ok']}, карточек: {nojs['quote_cards']}, «Goodreads»: {nojs['goodreads_links']}, ссылка Next: {nojs['next_link']}"),
        ("robots.txt", f"HTTP {d['robots_txt_status']}"),
        ("Вывод", d["conclusion"]["text"]),
    ]
    return pd.DataFrame(rows, columns=["Проверка", "Результат"])


def nocode_rows() -> int | None:
    p = C.OUT_DIR / "nocode" / "nocode_result.csv"
    if not p.exists():
        return None
    return int(len(pd.read_csv(p, keep_default_na=False)))


def comparison_table() -> pd.DataFrame:
    b, l = jload("browser.json"), jload("light.json")
    nrows = nocode_rows()
    spr = lambda m: m.get("sec_per_record")
    rows = [
        ("Время разработки, мин", FILL, FILL, FILL),
        ("Записей собрано", fmt(b.get("records")), fmt(l.get("records")),
         f"{nrows} (лента, 1 запись = цитата)" if nrows is not None else FILL),
        ("Время одного прогона, с", fmt(b.get("duration_s")), fmt(l.get("duration_s")), f"{FILL} (n8n → Executions)"),
        ("  из них паузы вежливости, с", fmt(b.get("paused_s")), fmt(l.get("paused_s")), "1 с × (страниц − 1)"),
        ("Число сетевых запросов", fmt(b.get("requests")), fmt(l.get("requests")), f"{FILL} (2 + число страниц)"),
        ("  заблокировано (route.abort)", fmt(b.get("blocked")), "—", "—"),
        ("Трафик, МБ", fmt(b.get("mb"), 3), fmt(l.get("mb"), 3), FILL),
        ("Пиковая память процесса, МБ", fmt(b.get("peak_rss_mb"), 1), fmt(l.get("peak_rss_mb"), 1),
         f"{FILL} (Монитор системы: node n8n)"),
        ("Секунд на запись", fmt(spr(b), 4), fmt(spr(l), 4), FILL),
        ("Устойчивость к редизайну (1–5)", "4", "4", "3"),
        ("Воспроизводимость (Git / экспорт)", "Git: код + requirements.txt", "Git: код + requirements.txt",
         "экспорт nocode/workflow.json + ручной запуск n8n"),
    ]
    return pd.DataFrame(rows, columns=["Метрика", "Playwright", "Дешёвая альтернатива (requests)", "No-code (n8n)"])


def volume_table() -> pd.DataFrame:
    rows = []
    for name, label in (("browser.json", "Playwright"), ("light.json", "requests")):
        m = jload(name)
        if not m:
            continue
        q = m.get("quality", {})
        rows.append({"подход": label, "записей (цитата×тег)": m.get("records"), "цитат в ленте": m.get("feed_quotes"),
                     "уникальных цитат в парах": q.get("unique_quotes"), "тегов (разделов)": q.get("unique_tags"),
                     "циклов (страниц)": m.get("cycles"), "сырых строк до дедупликации": m.get("raw_rows"),
                     "дубликатов по ключу": q.get("duplicates_by_key"), "сбоев": m.get("failures"),
                     "повторных входов": m.get("reauths"), "вход": (m.get("auth") or {}).get("mode")})
    return pd.DataFrame(rows)


def log_summary() -> pd.DataFrame:
    if not C.LOG_PATH.exists():
        return pd.DataFrame()
    log = pd.read_csv(C.LOG_PATH, keep_default_na=False)
    log = log[log["stage"] != "auth"].copy()
    for c in ("duration_s", "n_elements", "new_records"):
        log[c] = pd.to_numeric(log[c], errors="coerce")
    g = log.groupby(["approach", "stage"]).agg(циклов=("iteration", "count"), разделов=("section", "nunique"),
                                                элементов=("n_elements", "sum"), прирост=("new_records", "sum"),
                                                сек_среднее=("duration_s", "mean"), сек_макс=("duration_s", "max"),
                                                не_ok=("status", lambda s: int((s != "ok").sum())))
    return g.round(3).reset_index()


def quality_table() -> pd.DataFrame:
    rows = []
    for fname in ("data_browser.csv", "data_light.csv"):
        p = C.OUT_DIR / fname
        if not p.exists():
            continue
        df = C.typed(pd.read_csv(p, keep_default_na=False))
        q = C.quality_report(df)
        warns = C.smoke_check(df, name=fname)
        rows.append({"набор": fname, "записей": q["records"], "дубликатов по record_key": q["duplicates_by_key"],
                     **{f"пусто {k}": f"{v:.1%}" for k, v in q["empty_share"].items()},
                     "smoke-check": "OK" if not warns else "; ".join(warns)})
    return pd.DataFrame(rows)


def bench_tables() -> dict[str, pd.DataFrame]:
    out = {}
    for name in ("bench_block", "bench_extract", "bench_auth"):
        p = C.METRICS / f"{name}.csv"
        if p.exists():
            out[name] = pd.read_csv(p)
    return out


def md(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return f"_нет данных — {FILL} после прогона_\n"
    return df.to_markdown(index=False) + "\n"


def main() -> int:
    parts = ["# Сводка измерений ЛР-4 (вариант 5)\n",
             f"Сгенерировано: {C.now_iso()} · BASE_URL: `{C.BASE_URL}`\n"]
    if C.BASE_URL != "https://quotes.toscrape.com":
        parts.append("> **Внимание:** измерения получены НЕ на реальном сайте (тестовый BASE_URL) — "
                     "в отчёт не переносить.\n")
    parts += ["## 1. Протокол диагностики\n", md(diagnostics_table()),
              "## 2. Объём и состав набора\n", md(volume_table()),
              "## 3. Журнал сбора (по этапам)\n", md(log_summary()),
              "## 4. Таблица сравнения (4.5)\n", md(comparison_table()),
              "Оценка устойчивости (1–5) — экспертная, обоснование в report.md, разделы 4 и 6.\n"]
    bench = jload("bench.json")
    tabs = bench_tables()
    parts.append("## 5. Замеры оптимизаций\n")
    if bench:
        blk, ext, au = bench.get("block", {}), bench.get("extract", {}), bench.get("auth", {})
        parts.append(f"- Блокировка ресурсов (медианы, {bench.get('reps')} повт. × {bench.get('pages')} стр.): "
                     f"без блокировки {blk.get('full', {}).get('seconds')} c / {blk.get('full', {}).get('kb')} КБ / "
                     f"{blk.get('full', {}).get('requests')} запросов; с блокировкой {blk.get('block', {}).get('seconds')} c / "
                     f"{blk.get('block', {}).get('kb')} КБ / {blk.get('block', {}).get('requests')} запросов; "
                     f"экономия времени {blk.get('time_saving_pct')} %, трафика {blk.get('traffic_saving_pct')} %.\n")
        parts.append(f"- Массовое извлечение: `eval_on_selector_all` {ext.get('bulk_ms_total')} мс против поэлементного "
                     f"{ext.get('per_element_ms_total')} мс на {bench.get('pages')} страницах (×{ext.get('speedup_x')}), "
                     f"записи совпадают: {ext.get('all_pages_same_records')}.\n")
        parts.append(f"- Авторизация: восстановление storage_state — медиана {au.get('restore', {}).get('median_s')} c, "
                     f"{au.get('restore', {}).get('median_requests')} запрос(ов); новый вход формой — "
                     f"{au.get('fresh', {}).get('median_s')} c, {au.get('fresh', {}).get('median_requests')} запросов.\n")
    for k, df in tabs.items():
        parts += [f"\n`metrics/{k}.csv`\n\n", md(df)]
    par = jload("parallel.json")
    parts.append("\n## 6. Параллельный сбор (бонус)\n")
    if par:
        s, p = par["sequential"], par["parallel"]
        parts.append(f"Тегов: {par['tags']}; K=1: {s['seconds']} c ({s['records']} записей, ошибок {len(s['errors'])}); "
                     f"K={p['workers']}: {p['seconds']} c ({p['records']} записей, ошибок {len(p['errors'])}); "
                     f"ускорение ×{par['speedup_x']}; наборы K=1 и K={p['workers']} совпадают: {par['same_records_seq_vs_par']}; "
                     f"совпадают с data_browser.csv: {par['same_as_data_browser']}.\n")
    else:
        parts.append(f"_{FILL}_\n")
    fl = jload("faults.json")
    parts.append("\n## 7. Подмена ответов route.fulfill (бонус)\n")
    parts.append("".join(f"- {'PASS' if c['passed'] else 'FAIL'} — {c['check']}\n" for c in fl.get("checks", [])) or f"_{FILL}_\n")
    parts.append("\n## 8. Качество данных и smoke-check\n")
    parts.append(md(quality_table()))
    cmp_ = jload("compare.json")
    parts.append("\n## 9. Сверка наборов\n")
    for k in ("pairs", "feed"):
        r = cmp_.get(k)
        if r:
            parts.append(f"- {k}: браузер {r['browser_records']}, requests {r['light_records']}, общих ключей {r['common_keys']}, "
                         f"только браузер {r['only_browser_count']}, только requests {r['only_light_count']}, "
                         f"расхождений полей {sum(v['count'] for v in r['field_mismatches'].values())} → "
                         f"{'совпадают' if r['identical'] else 'есть расхождения'}\n")
    for k in ("consistency_browser", "consistency_light"):
        r = cmp_.get(k)
        if r:
            parts.append(f"- {k}: тегов в ленте {r['tags_in_feed']}, обойдено {r['tags_crawled']}; пар по метатегам ленты "
                         f"{r['expected_pairs_from_feed']}, на страницах тегов {r['pairs_from_tag_pages']} "
                         f"(не найдено {r['missing_on_tag_pages']}, лишних {r['unexpected_on_tag_pages']}); "
                         f"цитат без тегов {r['feed_quotes_without_tags']}\n")
    if cmp_.get("nocode"):
        parts.append(f"- no-code ↔ лента браузера: {cmp_['nocode']}\n")
    out = C.METRICS / "summary.md"
    out.write_text("\n".join(parts), encoding="utf-8")
    print(f"Сохранено: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
