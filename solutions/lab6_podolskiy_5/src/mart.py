"""Слой mart: аналитические представления, партиционированные снапшоты, графики.

    python -m src.mart          # предварительно должен пройти «светофор» (src/quality.py)

* mart/views/*.parquet — три представления из SQL-видов warehouse.db
  (v_repo_activity, v_weekly_activity, v_top_repos) + медиана времени закрытия;
* mart/snapshots/dt=YYYY-MM-DD/ — состояние репозиториев на дату сбора (Hive-партиции);
  повторный прогон в тот же день перезаписывает только свою партицию;
* mart/charts/*.png — графики по итоговым данным.
"""
from __future__ import annotations

import json
import sqlite3

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.dataset as ds  # noqa: E402

from . import config, quality  # noqa: E402

# Палитра: фиксированный порядок категориальных цветов (проверенная CVD-безопасная тройка)
C_ISSUE, C_PR, C_RELEASE = "#2a78d6", "#eb6834", "#1baf7a"
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"


def _style(ax, title: str, subtitle: str) -> None:
    ax.set_title(title, loc="left", fontsize=12, color=INK, pad=22)
    ax.text(0, 1.02, subtitle, transform=ax.transAxes, fontsize=9, color=MUTED)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.grid(axis="y", color=GRID, linewidth=1)
    ax.set_axisbelow(True)


def build_views(conn: sqlite3.Connection, events: pd.DataFrame) -> dict[str, pd.DataFrame]:
    activity = pd.read_sql_query("SELECT * FROM v_repo_activity ORDER BY events_total DESC", conn)
    closed = events[events["closed_at"].notna() & (events["event_type"] != "release")]
    hours = (closed["closed_at"] - closed["created_at"]).dt.total_seconds() / 3600
    med = hours.groupby(closed["repo_key"]).median().round(1).rename("median_hours_to_close")
    activity = activity.merge(med, left_on="repo_key", right_index=True, how="left")

    weekly = pd.read_sql_query("SELECT * FROM v_weekly_activity ORDER BY week_start, event_type", conn)
    weekly["week_start"] = pd.to_datetime(weekly["week_start"], utc=True)
    top = pd.read_sql_query("SELECT * FROM v_top_repos ORDER BY rank_by_stars", conn)
    return {"repo_activity": activity, "weekly_activity": weekly, "top_repos": top}


def write_snapshot(conn: sqlite3.Connection) -> tuple[str, int]:
    snap = pd.read_sql_query("""
        SELECT r.repo_key, r.stars, r.forks, r.watchers, r.open_issues_prs, r.releases_count, r.used_by,
               r.contributors, r.collected_at AS snapshot_at,
               SUM(e.event_type = 'issue' AND e.state = 'open')        AS issues_open_seen,
               SUM(e.event_type = 'pull_request' AND e.state = 'open') AS prs_open_seen,
               COUNT(e.event_key)                                      AS events_seen
        FROM repositories r LEFT JOIN events e ON e.repo_key = r.repo_key
        GROUP BY r.repo_key""", conn)
    snap["snapshot_at"] = pd.to_datetime(snap["snapshot_at"], utc=True)
    dt = snap["snapshot_at"].max().strftime("%Y-%m-%d")       # дата сбора (UTC) = ключ партиции
    snap["dt"] = dt
    table = pa.Table.from_pandas(snap, preserve_index=False)
    ds.write_dataset(table, config.MART_DIR / "snapshots", format="parquet",
                     partitioning=ds.partitioning(pa.schema([("dt", pa.string())]), flavor="hive"),
                     existing_data_behavior="delete_matching",      # перезаписываем только свою партицию
                     basename_template="part-{i}.parquet")
    return dt, len(snap)


def read_partition(dt: str) -> pd.DataFrame:
    """Чтение одной партиции без чтения остальных (фильтр по ключу партиции)."""
    return pd.read_parquet(config.MART_DIR / "snapshots", filters=[("dt", "=", dt)])


def charts(views: dict[str, pd.DataFrame], events: pd.DataFrame) -> list[str]:
    out_dir = config.MART_DIR / "charts"
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []

    # 1. Динамика: новые issues и PR по неделям за последние 26 недель
    w = views["weekly_activity"]
    w = w[w["event_type"] != "release"]
    if len(w):
        last = w["week_start"].max()
        w = w[w["week_start"] > last - pd.Timedelta(weeks=26)]
        piv = w.pivot_table(index="week_start", columns="event_type", values="created", aggfunc="sum").fillna(0)
        fig, ax = plt.subplots(figsize=(8, 4), dpi=150)
        for col, color, label in (("issue", C_ISSUE, "issues"), ("pull_request", C_PR, "pull requests")):
            if col in piv.columns:
                ax.plot(piv.index, piv[col], color=color, linewidth=2, solid_capstyle="round", label=label)
                ax.plot(piv.index[-1], piv[col].iloc[-1], "o", color=color, markersize=6,
                        markeredgecolor="white", markeredgewidth=2)
                ax.annotate(f"{int(piv[col].iloc[-1])}", (piv.index[-1], piv[col].iloc[-1]),
                            xytext=(6, 0), textcoords="offset points", va="center", fontsize=8, color=INK)
        _style(ax, "Новые issues и pull requests по неделям",
               "все репозитории набора, UTC, неделя с понедельника; последняя неделя может быть неполной")
        ax.set_ylabel("создано за неделю", color=MUTED, fontsize=9)
        ax.set_ylim(bottom=0)
        ax.legend(frameon=False, fontsize=8, loc="upper left")
        fig.autofmt_xdate()
        fig.tight_layout()
        path = out_dir / "weekly_activity.png"
        fig.savefig(path)
        plt.close(fig)
        files.append(path.name)

    # 2. Распределение времени до закрытия (лог-шкала): issues против PR
    closed = events[events["closed_at"].notna() & (events["event_type"] != "release")]
    if len(closed):
        hours = ((closed["closed_at"] - closed["created_at"]).dt.total_seconds() / 3600).clip(lower=0.1)
        bins = [10 ** (x / 4) for x in range(-4, 21)]                     # 0.1 ч … 100 000 ч
        fig, ax = plt.subplots(figsize=(8, 4), dpi=150)
        for col, color, label in (("issue", C_ISSUE, "issues"), ("pull_request", C_PR, "pull requests")):
            h = hours[closed["event_type"] == col]
            if len(h):
                ax.hist(h, bins=bins, histtype="step", linewidth=2, color=color,
                        label=f"{label}: медиана {h.median():.0f} ч (n={len(h)})")
        ax.set_xscale("log")
        for x, txt in ((1, "1 час"), (24, "сутки"), (24 * 7, "неделя"), (24 * 30, "месяц"), (24 * 365, "год")):
            ax.axvline(x, color=GRID, linewidth=1, zorder=0)
            ax.text(x, 0.98, f" {txt}", transform=ax.get_xaxis_transform(), fontsize=7, color=MUTED,
                    ha="left", va="top")
        _style(ax, "Сколько живёт issue и pull request до закрытия", "закрытые события, часы, логарифмическая шкала")
        ax.set_xlabel("часов от создания до закрытия", color=MUTED, fontsize=9)
        ax.set_ylabel("событий", color=MUTED, fontsize=9)
        ax.legend(frameon=False, fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=2)
        fig.tight_layout()
        path = out_dir / "time_to_close.png"
        fig.savefig(path)
        plt.close(fig)
        files.append(path.name)

    # 3. Состав активности по репозиториям (горизонтальный стек, 2px зазор между сегментами)
    a = views["repo_activity"].sort_values("events_total")
    if len(a):
        fig, ax = plt.subplots(figsize=(8, 4.6), dpi=150)
        left = pd.Series(0.0, index=a.index)
        for col, color, label in (("issues", C_ISSUE, "issues"), ("pull_requests", C_PR, "pull requests"),
                                  ("releases_seen", C_RELEASE, "releases")):
            vals = a[col].fillna(0).astype(float)
            ax.barh(a["repo_key"], vals, left=left, color=color, height=0.6, label=label,
                    edgecolor="white", linewidth=2)
            left += vals
        for y, total in enumerate(a["events_total"]):
            ax.text(total, y, f" {int(total)}", va="center", fontsize=7, color=INK)
        _style(ax, "Состав собранных событий по репозиториям", "issues и PR — API, релизы — HTML-страницы")
        ax.grid(axis="y", visible=False)
        ax.grid(axis="x", color=GRID, linewidth=1)
        ax.legend(frameon=False, fontsize=8, loc="upper center", bbox_to_anchor=(0.4, -0.06), ncol=3)
        fig.tight_layout()
        path = out_dir / "events_by_repo.png"
        fig.savefig(path)
        plt.close(fig)
        files.append(path.name)
    return files


def build(report: dict | None = None) -> dict:
    """Строит mart, только если светофор не красный."""
    report = report or quality.run(record=False)
    quality.enforce_gate(report)                  # красный → QualityGateError, mart не трогаем
    events = pd.read_parquet(config.CLEAN_EVENTS)
    conn = sqlite3.connect(config.WAREHOUSE_DB)
    views = build_views(conn, events)
    (config.MART_DIR / "views").mkdir(parents=True, exist_ok=True)
    for name, df in views.items():
        df.to_parquet(config.MART_DIR / "views" / f"{name}.parquet", index=False)
    dt, n = write_snapshot(conn)
    conn.close()
    files = charts(views, events)
    parts = sorted(p.name for p in (config.MART_DIR / "snapshots").iterdir() if p.is_dir())
    one = read_partition(dt)
    out = {"views": {k: len(v) for k, v in views.items()}, "snapshot_dt": dt, "snapshot_rows": n,
           "partitions": parts, "read_one_partition_rows": len(one), "charts": files, "verdict": report["verdict"]}
    (config.LOG_DIR / "mart_summary.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> int:
    try:
        out = build()
    except quality.QualityGateError as exc:
        print("⛔", exc)
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
