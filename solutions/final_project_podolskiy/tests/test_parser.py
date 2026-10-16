"""Тесты парсера страницы темы и robots.txt на сохранённых фикстурах (tests/fixtures/).

Фикстуры страницы — синтетические (см. demo/mock_github.py --dump-fixtures). После первого реального прогона
добавьте реальную страницу из raw/html_pages/<topic>/p01_<run>.html.gz:
    python -c "import gzip,shutil;shutil.copyfileobj(gzip.open('raw/html_pages/web-scraping/p01_<run>.html.gz'),open('tests/fixtures/real_topic_p1.html','wb'))"
и тест test_real_page_if_present проверит её автоматически.
"""
import json

import pytest

from collect_html import detect_block, parse_count, parse_topic_page, topic_url
from conftest import FIXTURES
from http_client import RobotsRules, backoff_delay, classify, CircuitBreaker

UA = "PSU-edu-crawler/1.0 (+https://github.com/Meta1rus/web-data-extraction; student lab)"


@pytest.fixture(scope="module")
def page1():
    return (FIXTURES / "topic_web-scraping_p1.html").read_text(encoding="utf-8")


def test_cards_count_keys_and_order(page1):
    cards, meta = parse_topic_page(page1, "web-scraping", 1)
    expected = json.loads((FIXTURES / "expected_p1.json").read_text(encoding="utf-8"))
    assert meta["strategy"] == "article-cards"
    assert meta["cards"] == 20
    assert [c["full_name"] for c in cards] == [e["full_name"] for e in expected]


def test_stars_exact_from_aria_label(page1):
    cards, _ = parse_topic_page(page1, "web-scraping", 1)
    expected = json.loads((FIXTURES / "expected_p1.json").read_text(encoding="utf-8"))
    assert [c["stars"] for c in cards] == [e["stars"] for e in expected]
    assert not any(c["stars_approx"] for c in cards)


def test_card_fields(page1):
    cards, meta = parse_topic_page(page1, "web-scraping", 1)
    c = cards[0]
    assert c["language"] == "Python"
    assert "web-scraping" in c["tags"]
    assert c["updated_raw"].endswith("Z")
    assert c["description"]
    assert meta["has_next"] is True
    assert meta["topic_total"] == 1336


def test_login_links_are_not_repos(page1):
    cards, _ = parse_topic_page(page1, "web-scraping", 1)
    assert all(not c["full_name"].lower().startswith("login/") for c in cards)


@pytest.mark.parametrize("text,value,approx", [
    ("71.3k", 71300, True), ("1,234", 1234, False), ("2.1m", 2100000, True), ("987", 987, False),
    ("", None, False), ("n/a", None, False), (None, None, False)])
def test_parse_count(text, value, approx):
    assert parse_count(text) == (value, approx)


def test_fallback_embedded_json():
    html = (FIXTURES / "topic_react_variant.html").read_text(encoding="utf-8")
    cards, meta = parse_topic_page(html, "web-scraping", 1)
    assert meta["strategy"] == "embedded-json"
    assert len(cards) == 5 and all(c["stars"] for c in cards)


def test_block_page_detected():
    html = (FIXTURES / "blocked_page.html").read_text(encoding="utf-8")
    cards, _ = parse_topic_page(html, "web-scraping", 1)
    verdict, signals = detect_block(200, html, len(cards))
    assert cards == [] and verdict == "блокировка" and signals


def test_normal_page_not_blocked(page1):
    cards, _ = parse_topic_page(page1, "web-scraping", 1)
    assert detect_block(200, page1, len(cards))[0] == "ok"


def test_topic_url():
    assert topic_url("https://github.com", "cli", 1, "python") == "https://github.com/topics/cli?l=python"
    assert topic_url("https://github.com", "cli", 3, "python") == "https://github.com/topics/cli?l=python&page=3"


def test_real_page_if_present():
    real = FIXTURES / "real_topic_p1.html"
    if not real.exists():
        pytest.skip("реальная страница ещё не сохранена (см. docstring модуля)")
    cards, meta = parse_topic_page(real.read_text(encoding="utf-8"), "web-scraping", 1)
    assert meta["strategy"] == "article-cards", "вёрстка изменилась: сработал фолбэк"
    assert len(cards) >= 10 and all(c["stars"] is not None for c in cards)


# ---------------- robots.txt: urllib.robotparser vs RFC 9309

@pytest.fixture(scope="module")
def robots():
    return RobotsRules((FIXTURES / "github_robots.txt").read_text(encoding="utf-8"), UA)


def test_robots_group_is_star(robots):
    assert robots.group_agent == "*"
    assert robots.crawl_delay is None


@pytest.mark.parametrize("path,allowed", [
    ("/topics/web-scraping?l=python&page=2", True),
    ("/topics/machine-learning", True),
    ("/scrapy/scrapy", True),
    ("/scrapy/scrapy/stargazers", False),
    ("/scrapy/scrapy/contributors", False),
    ("/scrapy/scrapy/tree/master", False),
    ("/scrapy/scrapy?tab=readme-ov-file", False),
    ("/search?q=scrapy", False),
    ("/topics/web-scraping?l=python&q=x", False),
])
def test_robots_rfc9309(robots, path, allowed):
    assert robots.can_fetch("https://github.com" + path)[0] is allowed


def test_urllib_robotparser_vs_rfc9309(robots):
    """urllib.robotparser в Python 3.10–3.12 не понимает * в путях и даёт ложное «можно» для
    /owner/repo/stargazers (проверено: 3.11.17, 3.12.3); в свежих выпусках 3.13 поддержка шаблонов есть
    (проверено: 3.13.16). Поэтому решение принимает собственный матчер RFC 9309, а результат стандартного
    парсера пишется рядом в отчёт для сравнения."""
    import sys
    from urllib import robotparser
    rp = robotparser.RobotFileParser()
    rp.parse((FIXTURES / "github_robots.txt").read_text(encoding="utf-8").splitlines())
    url = "https://github.com/scrapy/scrapy/stargazers"
    std = rp.can_fetch(UA, url)
    assert robots.can_fetch(url)[0] is False
    if sys.version_info < (3, 13):
        assert std is True        # известное ограничение старых версий
    assert rp.can_fetch(UA, "https://github.com/topics/web-scraping?l=python&page=2") is True


# ---------------- политика повторов и предохранитель

@pytest.mark.parametrize("status,retry", [(200, False), (304, False), (400, False), (401, False), (403, False),
                                          (404, False), (429, True), (500, True), (503, True)])
def test_classify(status, retry):
    assert classify(status)[1] is retry


def test_classify_github_primary_limit():
    assert classify(403, headers={"X-RateLimit-Remaining": "0"}) == ("rate_limit_primary", True)
    assert classify(403, body_snippet="You have exceeded a secondary rate limit")[0] == "rate_limit_secondary"


def test_backoff_bounded():
    assert all(0 <= backoff_delay(a, 1.0, 30.0) <= min(30.0, 2 ** a) for a in range(8) for _ in range(20))


def test_breaker_opens_and_half_opens():
    br = CircuitBreaker(fail_threshold=3, reset_after_s=0.0)
    for _ in range(3):
        br.record(False)
    assert br.state == "open" and br.activations == 1
    assert br.allow() is True and br.state == "half-open"
    br.record(True)
    assert br.state == "closed"


def test_text_fallbacks_for_stars_and_date():
    """Если у карточки нет счётчика с aria-label и <relative-time datetime>, берём текст «Star 71.3k» и «Updated Jul 25, 2026»."""
    html = """<html><body><article><h3><a href="/fx-a">fx-a</a> / <a href="/fx-a/tool">tool</a></h3>
    <a href="/login?return_to=%2Ffx-a%2Ftool">Star 71.3k</a><p>Desc</p>
    <a class="topic-tag" href="/topics/cli">cli</a><span itemprop="programmingLanguage">Python</span>
    <span>Updated Jul 25, 2026</span></article></body></html>"""
    cards, meta = parse_topic_page(html, "cli", 1)
    assert meta["strategy"] == "article-cards" and len(cards) == 1
    c = cards[0]
    assert c["full_name"] == "fx-a/tool"
    assert (c["stars"], c["stars_approx"]) == (71300, True)
    assert c["updated_raw"].startswith("2026-07-25")
