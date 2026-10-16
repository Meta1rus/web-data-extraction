"""Парсеры HTML, числа со страницы, ключи и robots.txt — на синтетических фикстурах."""
from pathlib import Path

import pytest

from src import parsers
from src.robots import RobotsPolicy

FIX = Path(__file__).resolve().parent / "fixtures"
UA = "PSU-edu-crawler/1.0 (+https://github.com/Meta1rus/web-data-extraction; student lab)"


@pytest.mark.parametrize("raw,expected", [
    ("54.3k", (54300, False)), ("53,912", (53912, False)), ("1.6m", (1600000, False)),
    ("5,000+", (5000, True)), ("970", (970, False)), (" 1.2k ", (1200, False)),
    ("—", (None, False)), ("54.3к", (None, False)), (None, (None, False)), ("", (None, False)),
])
def test_compact_number(raw, expected):
    assert parsers.parse_compact_number(raw) == expected


def test_keys_and_urls():
    assert parsers.repo_slug("https://github.com/Scrapy/Scrapy/?utm_source=x") == "scrapy/scrapy"
    assert parsers.repo_slug("https://api.github.com/REPOS/PSF/Requests") == "psf/requests"
    assert parsers.repo_slug("n8n-io/n8n.git") == "n8n-io/n8n"
    assert parsers.repo_slug("not a slug") is None
    assert parsers.canonical_url("https://GitHub.com/psf/requests/issues/12/?utm_source=a&x=1#c") == \
        "https://github.com/psf/requests/issues/12?x=1"
    assert parsers.issue_number_from_url("https://github.com/a/b/pull/77?x") == 77
    assert parsers.normalize_text("  a ​b \n c ") == "a b c"


@pytest.mark.parametrize("page", sorted(p.name for p in (FIX / "html").glob("*.html") if "__releases" not in p.name))
def test_repo_page(page):
    d = parsers.parse_repo_page((FIX / "html" / page).read_text(encoding="utf-8"))
    owner, repo = page[:-5].split("__")
    assert d["repo_nwo_raw"].lower() == f"{owner}/{repo}".lower()
    assert d["repository_id_raw"].isdigit()
    assert d["about_raw"] and d["stars_raw"] and d["releases_count_raw"]
    assert parsers.parse_compact_number(d["stars_raw"])[0] > 0
    assert d["languages_raw"] and parsers.split_language(d["languages_raw"][0])[1] is not None
    assert d["latest_release_tag_raw"] and d["latest_release_date_raw"].endswith("Z")


def test_repo_page_fallbacks():
    parsel = parsers.parse_repo_page((FIX / "html" / "scrapy__parsel.html").read_text(encoding="utf-8"))
    assert parsel["stars_title_raw"] is None and parsel["stars_raw"] == "1.3k"       # нет счётчика в шапке
    pw = parsers.parse_repo_page((FIX / "html" / "microsoft__playwright-python.html").read_text(encoding="utf-8"))
    assert pw["about_from"] == "meta"                                                # описание из <meta>
    httpx = parsers.parse_repo_page((FIX / "html" / "encode__httpx.html").read_text(encoding="utf-8"))
    assert httpx["languages_raw"][0].startswith("Python")                           # только полоска Progress
    n8n = parsers.parse_repo_page((FIX / "html" / "n8n-io__n8n.html").read_text(encoding="utf-8"))
    assert n8n["used_by_raw"] is None                                                # блока «Used by» нет


def test_releases_page_and_fallback():
    rels, nxt = parsers.parse_releases_page((FIX / "html" / "n8n-io__n8n__releases_p1.html").read_text(encoding="utf-8"))
    assert len(rels) == 10 and nxt.endswith("page=2")
    assert rels[0]["tag_raw"].startswith("n8n@")                                     # %40 раскодирован
    assert "Latest" in rels[0]["labels_raw"]
    assert any("Pre-release" in r["labels_raw"] for r in rels)
    arrow, _ = parsers.parse_releases_page((FIX / "html" / "apache__arrow__releases_p1.html").read_text(encoding="utf-8"))
    assert len(arrow) == 10 and all(r["datetime_raw"] and r["commit_sha_raw"] for r in arrow)   # без <section>


def test_robots_policy():
    policy = RobotsPolicy("https://github.com/robots.txt", 200, (FIX / "robots.txt").read_text(), UA)
    allowed = lambda p: policy.check("https://github.com" + p)["allowed"]
    assert allowed("/scrapy/scrapy") and allowed("/scrapy/scrapy/releases")
    for p in ("/scrapy/scrapy/contributors", "/scrapy/scrapy/stargazers", "/scrapy/scrapy/network/dependents",
              "/scrapy/scrapy/tree/main", "/scrapy/scrapy?tab=readme-ov-file", "/x/y.git"):
        assert not allowed(p), p
    assert RobotsPolicy("u", 503, "", UA).check("https://github.com/a/b")["allowed"] is False   # RFC 9309: 5xx
    assert RobotsPolicy("u", 404, "", UA).check("https://github.com/a/b")["allowed"] is True    # RFC 9309: 4xx
