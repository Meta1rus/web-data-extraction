"""Единая точка входа пайплайна: robots.txt -> HTML -> API -> raw -> clean -> БД (UPSERT) -> качество -> mart
-> отчёт о прогоне и оповещения.

Запуск:  python src/run_pipeline.py                       # полный прогон по config.yaml
         python src/run_pipeline.py --topics web-scraping --max-pages 1 --refresh-max-age-hours 0
                                                           # быстрый инкрементальный прогон (демо 304)
Коды возврата:
  0   — прогон завершён, вердикт зелёный или жёлтый;
  2   — КРАСНЫЙ статус качества: mart не обновлялся;
  3   — источник недоступен/заблокирован: отказ изолирован, прогон деградировал, записан инцидент;
  4   — сработал предохранитель max_requests_per_run (состояние сохранено, следующий прогон продолжит);
  5   — данные загружены, но сработало красное оповещение (обвал объёма, 403/429, доля ошибок) — разобраться;
  130 — прогон прерван (Ctrl+C или --simulate-crash-after): следующий запуск продолжит с места остановки;
  1   — непредвиденная ошибка (трассировка — в журнале и в logs/incidents/).
Последняя строка вывода — RUN_SUMMARY {...} (JSON) для узла n8n.
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import storage                                                      # noqa: E402
from clean import build_clean                                        # noqa: E402
from collect_api import collect_repos, resolve_mode                  # noqa: E402
from collect_html import SOURCE as HTML_SOURCE, collect_topics, topic_url  # noqa: E402
from http_client import EtagStore, PoliteClient, RequestBudgetExceeded, SimulatedCrash  # noqa: E402
from observability import (RunContext, evaluate_alerts, format_report, overall_verdict,  # noqa: E402
                           previous_report, read_log, request_stats, write_run_report)
from quality import compute_quality, format_quality                  # noqa: E402
from settings import (DIRS, ensure_dirs, github_token, iso, load_config, mask_secret,  # noqa: E402
                      parse_iso, read_json, utcnow, write_json_atomic)

CHECKPOINT = DIRS["state"] / "checkpoint.json"
WATERMARK = DIRS["state"] / "watermark.json"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Пайплайн: популярность и активность Python-репозиториев GitHub")
    ap.add_argument("--config", default=None, help="путь к config.yaml")
    ap.add_argument("--topics", default=None, help="подмножество тем через запятую (для демонстрации)")
    ap.add_argument("--max-pages", type=int, default=None, help="страниц на тему (по умолчанию из config)")
    ap.add_argument("--refresh-max-age-hours", type=float, default=None,
                    help="водяной знак: перепроверять репозитории старше N часов (0 — все, с ETag)")
    ap.add_argument("--skip-html", action="store_true", help="не обращаться к HTML (ключи — из БД)")
    ap.add_argument("--skip-api", action="store_true", help="не обращаться к API")
    ap.add_argument("--fresh", action="store_true", help="игнорировать незавершённый прогон (не возобновлять)")
    ap.add_argument("--debug-cache", action="store_true", help="кэш HTML на диске (только для отладки парсера)")
    ap.add_argument("--rate-scale", type=float, default=1.0,
                    help="ускорение лимитов; разрешено только для локальной имитации 127.0.0.1")
    ap.add_argument("--simulate-crash-after", type=int, default=None,
                    help="имитировать обрыв после N сетевых запросов (демо возобновляемости)")
    ap.add_argument("--quiet", action="store_true")
    return ap.parse_args(argv)


def load_checkpoint(cfg: dict, ctx: RunContext, fresh: bool) -> tuple[dict, str | None]:
    cp = read_json(CHECKPOINT, None)
    if cp and cp.get("status") == "running" and not fresh:
        started = parse_iso(cp.get("started_at"))
        age_h = (utcnow() - started).total_seconds() / 3600 if started else 1e9
        if age_h <= float(cfg["resume"]["max_age_hours"]):
            pages = sum(len(v) for v in cp.get("html_done", {}).values())
            ctx.log("resume", from_run=cp.get("run_id"), html_pages_done=pages,
                    api_keys_done=len(cp.get("api_done", [])))
            ctx.say(f"Возобновление прерванного прогона {cp.get('run_id')}: страниц HTML уже собрано {pages}, "
                    f"ключей API обработано {len(cp.get('api_done', []))}")
            cp.setdefault("resumed_by", []).append(ctx.run_id)
            return cp, cp.get("run_id")
        ctx.log("resume_expired", from_run=cp.get("run_id"), age_hours=round(age_h, 1))
    return {"run_id": ctx.run_id, "started_at": iso(ctx.started_at), "status": "running",
            "html_done": {}, "api_done": []}, None


def main(argv=None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)
    ensure_dirs()
    ctx = RunContext(echo=not args.quiet)
    token = github_token()
    html_cfg, api_cfg = cfg["sources"]["html"], cfg["sources"]["api"]
    topics = [t.strip() for t in args.topics.split(",")] if args.topics else list(html_cfg["topics"])
    scope = "|".join([",".join(sorted(topics)), str(args.max_pages or html_cfg["max_pages_per_topic"]),
                      ",".join(html_cfg.get("sort_modes", ["stars"])), "html" if not args.skip_html else "no-html"])
    ctx.log("run_started", argv=sys.argv[1:], html_base=html_cfg["base_url"], api_base=api_cfg["base_url"],
            token=mask_secret(token), api_mode=resolve_mode(cfg, token), python=sys.version.split()[0],
            topics=topics, user_agent=cfg["http"]["user_agent"])
    ctx.say(f"Прогон {ctx.run_id}: HTML {html_cfg['base_url']} | API {api_cfg['base_url']} | "
            f"токен: {mask_secret(token)} | режим API: {resolve_mode(cfg, token)}")

    checkpoint, resumed_from = load_checkpoint(cfg, ctx, args.fresh)
    write_json_atomic(CHECKPOINT, checkpoint)
    conn = storage.connect(cfg)
    storage.run_started(conn, ctx, cfg, resumed_from)
    state = read_json(WATERMARK, {"version": 1, "repos": {}})
    etags = EtagStore()
    client = PoliteClient(cfg, ctx, etags, rate_scale=args.rate_scale,
                          simulate_crash_after=args.simulate_crash_after,
                          debug_cache=args.debug_cache or bool(cfg["http"].get("debug_cache")))
    client.register_limit("html", html_cfg["base_url"], html_cfg["rate_per_sec"], html_cfg["burst"])
    client.register_limit("api", api_cfg["base_url"], api_cfg["rate_per_sec"], api_cfg["burst"])
    client.register_limit("api_search", api_cfg["base_url"], api_cfg["search_rate_per_sec"], 1)

    def save_checkpoint() -> None:
        write_json_atomic(CHECKPOINT, checkpoint)

    records: dict = {"resumed_from": resumed_from}
    report: dict = {"run_id": ctx.run_id, "started_at": iso(ctx.started_at), "records": records,
                    "environment": {"html_base": html_cfg["base_url"], "api_base": api_cfg["base_url"],
                                    "api_mode": resolve_mode(cfg, token), "token": mask_secret(token),
                                    "python": sys.version.split()[0], "topics": topics,
                                    "parser_version": cfg["project"]["parser_version"], "scope": scope}}
    status, exit_code = "failed", 1
    source_failed = False
    quality = None
    try:
        # ---------- 1. robots.txt (только для HTML-сайта; для API действуют документация и лимиты)
        html_allowed = not args.skip_html
        if html_allowed and html_cfg.get("check_robots", True):
            probes = [topic_url(html_cfg["base_url"], t, 2, html_cfg.get("language_filter")) for t in topics[:2]]
            probes += [html_cfg["base_url"] + "/scrapy/scrapy", html_cfg["base_url"] + "/scrapy/scrapy/stargazers",
                       html_cfg["base_url"] + "/scrapy/scrapy?tab=readme-ov-file"]
            robots = client.check_robots(html_cfg["base_url"], probes)
            report["robots"] = robots.as_dict()
            if robots.status is None or (robots.status >= 500):
                # RFC 9309: robots.txt недоступен из-за сбоя сервера/сети -> считать сайт полностью закрытым
                html_allowed = False
                source_failed = True
                ctx.incident("source_unavailable", f"robots.txt недоступен (код {robots.status}): HTML-источник "
                             "пропущен в этом прогоне (RFC 9309: при недоступности — полный запрет)",
                             source=HTML_SOURCE)
            elif robots.crawl_delay:
                client.register_limit("html", html_cfg["base_url"], html_cfg["rate_per_sec"], html_cfg["burst"],
                                      crawl_delay=robots.crawl_delay)
            ctx.say(f"robots.txt: код {robots.status}, группа {robots.group!r}, Crawl-delay {robots.crawl_delay}; "
                    + "; ".join(f"{c['url'].split('//', 1)[-1]} -> {'можно' if c['rfc9309'] else 'нельзя'}"
                                for c in robots.checks))

        # ---------- 2. HTML: страницы тем
        listing_index: dict[str, dict] = {}
        html_stats = {"html_pages_ok": 0, "html_cards": 0, "html_pages_failed": 0}
        if html_allowed:
            ctx.say("Сбор HTML: " + ", ".join(topics))
            html_stats = collect_topics(client, cfg, ctx, checkpoint, topics, args.max_pages, save_checkpoint)
            for env in html_stats.pop("listings"):
                p = env["payload"]
                listing_index[p["full_name"].lower()] = {"full_name": p["full_name"],
                                                         "html_updated_at": p.get("updated_raw")}
            pages_total = html_stats["html_pages_ok"] + html_stats.get("html_pages_resumed", 0)
            if pages_total == 0 and (html_stats["html_pages_failed"] or html_stats["html_blocked"]):
                source_failed = True
                ctx.incident("source_unavailable", "HTML-источник не отдал ни одной страницы — отказ изолирован, "
                             "ключи для API берутся из ранее собранных карточек", source=HTML_SOURCE)
            elif html_stats.get("html_blocked"):
                source_failed = True
        records.update({k: v for k, v in html_stats.items() if k != "topics"})
        report["html_topics"] = html_stats.get("topics", {})
        if not listing_index:
            known = storage.listings_from_db(conn, topics, int(cfg["storage"]["listing_max_age_days"]))
            listing_index = {r["repo_key"]: {"full_name": r["full_name"], "html_updated_at": r["html_updated_at"]}
                             for r in known}
            records["api_keys_from_db"] = len(listing_index)
            if known:
                ctx.say(f"Деградация: ключи для API взяты из БД ({len(known)} карточек)")

        # ---------- 3. API: обогащение
        verified: dict[str, str] = {}
        if not args.skip_api and listing_index:
            api_stats = collect_repos(client, cfg, ctx, listing_index, state, checkpoint, token, topics,
                                      args.refresh_max_age_hours, save_checkpoint)
            how = "search" if api_stats["api_mode"] == "search" else None
            for k in api_stats.pop("verified_keys"):
                verified[k] = how or str(state["repos"].get(k, {}).get("status", 200))
            records.update({k: v for k, v in api_stats.items() if k != "api_reasons"})
            records["api_reasons"] = api_stats["api_reasons"]
            if api_stats["api_source_unavailable"]:
                source_failed = True
                ctx.incident("source_unavailable", f"GitHub API недоступен: предохранитель разомкнут, пропущено "
                             f"{api_stats['api_skipped_breaker']} запросов; в анализ идут ранее собранные метрики",
                             source="github_api_repos")
        state["last_run_id"] = ctx.run_id
        write_json_atomic(WATERMARK, state)
        etags.save()

        # ---------- 4. очистка и контракт
        clean = build_clean(cfg, ctx)
        records.update({"clean_repos": len(clean["repos"]), "clean_listings": len(clean["listings"]),
                        "rejects": len(clean["rejects"])})
        report["cleaning_protocol"] = clean["protocol"]
        ctx.say(f"Чистый слой: репозиториев {len(clean['repos'])}, карточек {len(clean['listings'])}, "
                f"отбраковано {len(clean['rejects'])}")

        # ---------- 5. хранилище (UPSERT)
        if not clean["repos"].empty or not clean["listings"].empty:
            report["storage"] = storage.load_all(conn, clean, ctx, verified)

        # ---------- 6. качество
        prev = previous_report(ctx.run_id)
        quality = compute_quality(clean, cfg, ctx, prev)
        records["analysis_repos"] = int(next(m["value"] for m in quality["metrics"]
                                             if m["metric"] == "clean_repos_count") or 0)
        report["quality"] = {"verdict": quality["verdict"], "metrics": quality["metrics"], "info": quality["info"],
                             "reject_reasons": quality["reject_reasons"]}
        ctx.say(format_quality(quality))

        # ---------- 7. mart (только если качество не красное)
        if quality["verdict"] != "red":
            marts = storage.build_mart(conn, cfg, ctx)
            report["mart_built"] = bool(marts)
            report["mart"] = {k: len(v) for k, v in marts.items()}
        else:
            report["mart_built"] = False
            ctx.log("mart_blocked", level="ERROR", reason="красный статус качества")
            ctx.say("Загрузка в mart НЕ выполнена: красный статус качества.")

        status = "finished"
        if source_failed:
            exit_code = 3
        elif quality["verdict"] == "red":
            exit_code = 2
        else:
            exit_code = 0
    except (SimulatedCrash, KeyboardInterrupt) as e:
        status, exit_code = "interrupted", 130
        ctx.log("run_interrupted", level="ERROR", reason=f"{type(e).__name__}: {e}")
        ctx.say(f"Прогон прерван ({e}). Состояние сохранено — следующий запуск продолжит с места остановки.")
    except RequestBudgetExceeded as e:
        status, exit_code = "interrupted", 4
        ctx.incident("request_budget", str(e), source="pipeline")
    except Exception as e:                                   # noqa: BLE001 — фиксируем любую ошибку в журнале
        status, exit_code = "failed", 1
        ctx.incident("pipeline_error", f"{type(e).__name__}: {e}", source="pipeline",
                     traceback=traceback.format_exc())
    finally:
        etags.save()
        write_json_atomic(WATERMARK, state)
        if status == "interrupted":
            write_json_atomic(CHECKPOINT, checkpoint)          # оставляем — следующий запуск возобновит
        else:
            checkpoint["status"] = status
            checkpoint["finished_by"] = ctx.run_id
            write_json_atomic(CHECKPOINT, checkpoint)

        log_rows = read_log(ctx.log_path)
        report.update({"finished_at": iso(utcnow()), "duration_s": ctx.elapsed_s(), "status": status,
                       "requests": request_stats(log_rows), "incidents": ctx.incidents})
        if "api_200" not in records:     # прогон оборвался на этапе API — считаем по журналу
            api_req = [r for r in log_rows if r.get("event") == "request" and r.get("source") == "github_api_repos"]
            records["api_200"] = sum(1 for r in api_req if r.get("status") == 200)
            records["api_304"] = sum(1 for r in api_req if r.get("status") == 304)
        history = storage.run_history(conn, ctx.run_id)
        alerts = evaluate_alerts(report, previous_report(ctx.run_id, scope), history, cfg)
        report["alerts"] = alerts
        verdict = overall_verdict(alerts)
        if status != "finished":
            verdict = "red"
        elif verdict == "red" and exit_code == 0:
            exit_code = 5          # красное оповещение (обвал объёма, 403/429, ошибки) при формально успешном прогоне
        report["verdict"] = verdict
        report["exit_code"] = exit_code
        report["report_path"] = f"reports/run_report_{ctx.run_id}.json"
        write_run_report(report)
        storage.run_finished(conn, ctx, report)
        conn.close()
        ctx.log("run_finished", status=status, verdict=verdict, exit_code=exit_code, duration_s=report["duration_s"])
        ctx.say("")
        ctx.say(format_report(report))
        summary = {"run_id": ctx.run_id, "status": status, "verdict": verdict, "exit_code": exit_code,
                   "alerts": [f"[{a['level']}] {a['message']}" for a in alerts],
                   "report_path": report["report_path"], "log_path": f"logs/{ctx.log_path.name}",
                   "html_cards": records.get("html_cards", 0), "api_200": records.get("api_200", 0),
                   "api_304": records.get("api_304", 0), "clean_repos": records.get("clean_repos", 0),
                   "inserted": report.get("storage", {}).get("repos", {}).get("inserted", 0),
                   "updated": report.get("storage", {}).get("repos", {}).get("updated", 0)}
        print("RUN_SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
