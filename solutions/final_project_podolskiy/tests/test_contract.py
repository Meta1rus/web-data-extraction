"""Тесты контракта данных (pydantic), очистки ответа API и идемпотентного UPSERT."""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from clean import add_computed, clean_text, repo_from_raw
from collect_api import minimize
from contract import RepoRecord, TopicListing, validate_records
from conftest import FIXTURES, ROOT

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def api_env(**patch):
    repo = json.loads((FIXTURES / "api_repo.json").read_text(encoding="utf-8"))
    repo.update(patch)
    return {"_run_id": "test", "_collected_at": "2026-10-15T12:00:00Z", "_endpoint": "repos",
            "_parser_version": "t", "requested_key": repo["full_name"].lower(), "payload": minimize(repo)}


def repo_record(**patch):
    rec = {k: v for k, v in repo_from_raw(api_env()).items() if k in RepoRecord.model_fields}
    rec.update(patch)
    return rec


def listing_record(**patch):
    rec = {"topic": "web-scraping", "repo_key": "fx-a/b", "full_name": "fx-a/b", "page": 1, "rank_in_topic": 1,
           "stars_html": 10, "stars_html_approx": False, "language_html": "Python", "description_html": "x",
           "tags": ["python", "web-scraping"], "html_updated_at": NOW - timedelta(days=1), "collected_at": NOW,
           "source": "github_topics_html", "parser_version": "t", "run_id": "test"}
    rec.update(patch)
    return rec


def test_minimize_drops_personal_and_url_fields():
    p = api_env()["payload"]
    assert set(p["owner"]) == {"login", "type"}
    assert "html_url" not in p and "url" not in p


def test_valid_repo_passes():
    RepoRecord(**repo_record())


@pytest.mark.parametrize("patch,field", [
    ({"stars": -1}, "stars"),
    ({"full_name": "bad name/repo", "repo_key": "bad name/repo"}, "full_name"),
    ({"topics": ["Web Scraping!"]}, "topics"),
    ({"license_spdx": "MIT License (custom)"}, "license_spdx"),
    ({"owner_type": "Bot"}, "owner_type"),
])
def test_repo_field_rules(patch, field):
    valid, rejects = validate_records([repo_record(**patch)], RepoRecord, "github_api_repos")
    assert not valid and rejects[0]["field"] == field and rejects[0]["reason"]


def test_repo_created_in_future_rejected():
    future = datetime(2027, 11, 1, tzinfo=timezone.utc)
    valid, rejects = validate_records([repo_record(created_at=future)], RepoRecord, "x")
    assert not valid and "created_at" in rejects[0]["reason"]


def test_repo_key_must_match_full_name():
    valid, rejects = validate_records([repo_record(repo_key="someone/else")], RepoRecord, "x")
    assert not valid and "repo_key" in rejects[0]["reason"]


def test_naive_datetime_rejected():
    valid, rejects = validate_records([repo_record(created_at=datetime(2020, 1, 1))], RepoRecord, "x")
    assert not valid and rejects[0]["field"] == "created_at"


def test_topics_normalized_and_deduplicated():
    r = RepoRecord(**repo_record(topics=["Python", "python", "web-scraping"]))
    assert r.topics == ["python", "web-scraping"]


def test_listing_valid_and_rules():
    TopicListing(**listing_record())
    bad = [listing_record(topic="Web Scraping"), listing_record(page=0),
           listing_record(html_updated_at=NOW + timedelta(days=2)), listing_record(repo_key="other/key")]
    valid, rejects = validate_records(bad, TopicListing, "github_topics_html")
    assert not valid and len(rejects) == 4
    assert all(r["reason"] and r["payload"] for r in rejects)


def test_clean_text_normalizes_invisible_and_nbsp():
    assert clean_text("  Fast  scraping​ toolkit\n with   retries ") == "Fast scraping toolkit with retries"
    assert clean_text("   ") is None and clean_text(None) is None


def test_computed_fields():
    rec = RepoRecord(**repo_record()).model_dump()
    df = add_computed(pd.DataFrame([rec]))
    assert df.loc[0, "size_band"] is not None and df.loc[0, "age_days"] > 0


def test_upsert_idempotent(tmp_path: Path):
    """Три прогона: вставка -> нули -> обновление одной записи; дублей нет."""
    import storage
    from observability import RunContext

    cfg = {"storage": {"db_path": str(tmp_path / "w.db"), "schema_path": "db/schema.sql"}}
    conn = storage.connect(cfg, tmp_path / "w.db")
    ctx = RunContext(run_id="pytest", echo=False)
    ctx.log_path = tmp_path / "log.jsonl"
    rec = RepoRecord(**repo_record()).model_dump()
    rows = storage.repo_rows(pd.DataFrame([rec, {**rec, "repo_key": "fx-z/other", "full_name": "fx-z/other",
                                                 "owner_login": "fx-z", "repo_id": rec["repo_id"] + 1}]),
                             "2026-10-15T12:00:00Z")
    cols, key = storage.REPO_COLS, ["repo_key"]
    r1 = storage.upsert(conn, "repos", cols, key, rows, ("collected_at",), ctx)
    r2 = storage.upsert(conn, "repos", cols, key, rows, ("collected_at",), ctx)
    rows[0] = {**rows[0], "stars": rows[0]["stars"] + 5}
    rows[0]["row_hash"] = storage._hash(rows[0], storage.REPO_BUSINESS)
    r3 = storage.upsert(conn, "repos", cols, key, rows, ("collected_at",), ctx)
    assert (r1["inserted"], r1["updated"]) == (2, 0)
    assert (r2["inserted"], r2["updated"], r2["unchanged"]) == (0, 0, 2)
    assert (r3["inserted"], r3["updated"], r3["unchanged"]) == (0, 1, 1)
    assert conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 2


def test_db_check_rejects_negative_stars(tmp_path: Path):
    import storage
    cfg = {"storage": {"db_path": str(tmp_path / "w.db"), "schema_path": "db/schema.sql"}}
    conn = storage.connect(cfg, tmp_path / "w.db")
    rec = RepoRecord(**repo_record()).model_dump()
    row = storage.repo_rows(pd.DataFrame([rec]), "2026-10-15T12:00:00Z")[0]
    row["stars"] = -5
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(storage._upsert_sql("repos", storage.REPO_COLS, ["repo_key"], ("first_seen_at",)), row)
