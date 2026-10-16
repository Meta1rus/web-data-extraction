"""End-to-end на локальной имитации GitHub: collect → corrupt → pipeline → fill_report.

Данные пишутся во временную папку (LAB6_DATA_ROOT), рабочие raw/clean/db лабораторной не трогаются.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

import mock_github

ROOT = Path(__file__).resolve().parent.parent


def run(args, env):
    r = subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert r.returncode in (0,), f"{args}: {r.returncode}\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}"
    return r.stdout


@pytest.fixture(scope="module")
def data_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("lab6_data")
    api, web, state, stop = mock_github.start(limit=60)
    env = {**os.environ, "LAB6_DATA_ROOT": str(root), "GITHUB_API_URL": api, "GITHUB_WEB_URL": web,
           "MPLBACKEND": "Agg"}
    env.pop("GITHUB_TOKEN", None)
    try:
        run(["-m", "src.collect", "--api-pause", "0.01", "--html-pause", "0.01"], env)
        run(["-m", "src.corrupt", "--share", "0.07", "--seed", "5"], env)
        run(["-m", "src.pipeline"], env)
        shutil.copy(ROOT / "report.md", root / "report.md")
        run(["-m", "src.fill_report", "--report", str(root / "report.md")], env)
    finally:
        stop()
    yield root


def test_raw_provenance(data_root):
    for name in ("github_api.jsonl", "github_html.jsonl"):
        for line in (data_root / "raw" / name).read_text(encoding="utf-8").splitlines()[:50]:
            rec = json.loads(line)
            for k in ("_source", "_source_url", "_collected_at", "_parser_version", "_status"):
                assert k in rec
            assert rec["_collected_at"].endswith("+00:00")


def test_clean_layer(data_root):
    ev = pd.read_parquet(data_root / "clean" / "dataset.parquet")
    repos = pd.read_parquet(data_root / "clean" / "repositories.parquet")
    assert len(ev) >= 300 and len(repos) == 12
    assert ev["event_key"].is_unique and repos["repo_key"].is_unique
    assert set(ev["source"]) == {"github_api", "github_html"}                  # два источника разной природы
    for c in ("created_at", "collected_at", "closed_at"):
        assert str(ev[c].dt.tz) == "UTC"
    assert not ev["number"].dropna().gt(9_000_000).any()                      # синтетика в clean не попала
    assert str(ev["comments"].dtype) == "Int64"


def test_rejects_only_synthetic(data_root):
    rej = [json.loads(x) for x in (data_root / "clean" / "rejects.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rej and all(r["synthetic"] for r in rej)
    assert all(r["reason"] and r["stage"] for r in rej)


def test_rebuild_identical(data_root):
    rb = json.loads((data_root / "logs" / "rebuild.json").read_text(encoding="utf-8"))
    assert rb["identical"] is True


def test_idempotency_and_constraints(data_root):
    demo = json.loads((data_root / "logs" / "load_demo.json").read_text(encoding="utf-8"))
    r1, r2, r3 = demo["runs"]
    assert r2["repos: обновлено"] == 0 and r2["events: обновлено"] == 0
    assert r2["строк с updated_at этого прогона"] == 0 and r2["новых версий в истории"] == 0
    assert r3["repos: обновлено"] + r3["events: обновлено"] == 2 and r3["новых версий в истории"] == 2
    assert r1["строк events"] == r2["строк events"] == r3["строк events"]
    assert all(c["результат"] == "отклонено базой" for c in demo["constraints"])
    assert demo["asof_between"]["event"][0]["state"] == "open"
    assert demo["asof_now"]["event"][0]["state"] == "closed"


def test_warehouse(data_root):
    with sqlite3.connect(data_root / "db" / "warehouse.db") as c:
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        n = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        bad = c.execute("SELECT COUNT(*) FROM events WHERE created_at NOT LIKE '%Z'").fetchone()[0]
        assert n >= 300 and bad == 0


def test_quality_and_mart(data_root):
    q = json.loads((data_root / "logs" / "quality_latest.json").read_text(encoding="utf-8"))
    assert q["verdict"] != "НЕ ПРИГОДЕН"
    red = json.loads((data_root / "logs" / "red_gate_demo.json").read_text(encoding="utf-8"))
    assert "остановлена" in red["gate"]
    assert len(list((data_root / "mart" / "views").glob("*.parquet"))) == 3
    assert len(list((data_root / "mart" / "charts").glob("*.png"))) >= 2
    assert any((data_root / "mart" / "snapshots").glob("dt=*/*.parquet"))


def test_report_filled_and_marked(data_root):
    text = (data_root / "report.md").read_text(encoding="utf-8")
    left = [line for line in text.splitlines() if "⟪ЗАПОЛНИТЬ" in line]
    assert all("n8n" in line for line in left)          # вручную остаются только числа прогона n8n
    assert "(на локальной имитации)" in text


def test_raw_tamper_detected(data_root, tmp_path):
    copy = tmp_path / "raw"
    shutil.copytree(data_root / "raw", copy)
    target = copy / "github_html.jsonl"
    os.chmod(target, 0o644)
    data = target.read_bytes()
    target.write_bytes(data.replace(b"scrapy", b"scrapz", 1))
    env = {**os.environ, "LAB6_DATA_ROOT": str(tmp_path)}
    r = subprocess.run([sys.executable, "-c", "from src import rawio; rawio.verify_all()"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "RawTamperedError" in r.stderr
