"""Контракт pydantic: опорная запись проходит, все пробы отклоняются с полем и сообщением."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.contract import SAMPLE_EVENT, Event, run_probes


def test_sample_event_valid():
    e = Event(**SAMPLE_EVENT)
    assert e.created_at.tzinfo is not None and e.created_at.utcoffset().total_seconds() == 0


def test_title_normalized():
    e = Event(**{**SAMPLE_EVENT, "title": "  Fix ​  bug \n"})
    assert e.title == "Fix bug"


def test_offset_converted_to_utc():
    from datetime import timedelta
    msk = timezone(timedelta(hours=3))
    e = Event(**{**SAMPLE_EVENT, "created_at": datetime(2026, 5, 1, 13, 0, tzinfo=msk)})
    assert e.created_at.hour == 10 and e.created_at.utcoffset().total_seconds() == 0


def test_all_probes_rejected():
    rows = run_probes()
    assert len(rows) >= 5
    assert all(r["результат"] == "отклонена" and r["сообщение"] for r in rows)


def test_release_rules():
    rel = {**SAMPLE_EVENT, "event_type": "release", "number": None, "tag": "2.11.2", "state": "released",
           "event_key": "scrapy/scrapy@2.11.2", "closed_at": None, "state_reason": None, "source": "github_html",
           "commit_sha": "0a1b2c3"}
    assert Event(**rel).commit_sha == "0a1b2c3"
    with pytest.raises(ValidationError):
        Event(**{**rel, "number": 5})
