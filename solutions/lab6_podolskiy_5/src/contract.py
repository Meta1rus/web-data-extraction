"""Контракт данных чистого слоя (pydantic v2): репозиторий 1 → N событий.

Две модели: Repository (справочник) и Event (issue, pull request, release).
Контракт применяется после шести шагов очистки — второй рубеж: шаги ловят
типовые проблемы набора, контракт проверяет каждую запись по отдельности.
Невалидная запись не выбрасывается молча: она уходит в rejects с указанием поля
и текста ошибки.
"""
from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .parsers import SLUG_RX, ZERO_WIDTH

CLOCK_SKEW = timedelta(minutes=5)       # допуск расхождения часов сервера и сборщика

AuthorAssociation = Literal["COLLABORATOR", "CONTRIBUTOR", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR",
                            "MANNEQUIN", "MEMBER", "NONE", "OWNER"]


def _as_utc(v: datetime | None) -> datetime | None:
    """Только осведомлённые о часовом поясе даты; результат — в UTC."""
    if v is None:
        return None
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError("дата без часового пояса (наивная) — источник времени неизвестен")
    return v.astimezone(timezone.utc)


def _http_url(v: str | None) -> str | None:
    if v is None:
        return None
    if not v.startswith(("http://", "https://")):
        raise ValueError("URL должен начинаться с http(s)://")
    if any(p in v for p in ("utm_", "fbclid=", "gclid=")):
        raise ValueError("URL содержит параметры трекинга — должен быть нормализован")
    return v


def _slug(v: str) -> str:
    if not SLUG_RX.match(v):
        raise ValueError("repo_key должен иметь вид owner/name в нижнем регистре")
    return v


class Repository(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_key: str = Field(min_length=3, max_length=140)
    github_id: int = Field(gt=0)
    owner: str = Field(min_length=1, max_length=39)
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    homepage: str | None = None
    primary_language: str | None = Field(default=None, max_length=60)
    license_spdx: str | None = Field(default=None, max_length=64)
    topics: list[str] = Field(default_factory=list, max_length=30)
    stars: int = Field(ge=0, le=10_000_000)
    forks: int = Field(ge=0, le=10_000_000)
    watchers: int | None = Field(default=None, ge=0, le=10_000_000)
    open_issues_prs: int = Field(ge=0, le=1_000_000)
    size_kb: int | None = Field(default=None, ge=0)
    default_branch: str | None = Field(default=None, max_length=255)
    archived: bool
    created_at: datetime
    gh_updated_at: datetime | None = None
    pushed_at: datetime | None = None
    # поля из HTML-страницы (нет в /repos/{o}/{r})
    releases_count: int | None = Field(default=None, ge=0, le=100_000)
    latest_release_tag: str | None = Field(default=None, min_length=1, max_length=200)
    latest_release_at: datetime | None = None
    used_by: int | None = Field(default=None, ge=0)
    contributors: int | None = Field(default=None, ge=0, le=1_000_000)
    contributors_is_lower_bound: bool | None = None
    primary_language_pct: float | None = Field(default=None, ge=0, le=100)
    languages: list[str] = Field(default_factory=list)
    stars_html: int | None = Field(default=None, ge=0)
    html_collected_at: datetime | None = None
    html_source_url: str | None = None
    # происхождение
    source: Literal["github_api", "github_api+github_html"]
    source_url: str
    parser_version: str = Field(min_length=3)
    collected_at: datetime

    @field_validator("repo_key")
    @classmethod
    def slug_key(cls, v):
        return _slug(v)

    @field_validator("created_at", "gh_updated_at", "pushed_at", "latest_release_at",
                     "html_collected_at", "collected_at")
    @classmethod
    def utc_only(cls, v):
        return _as_utc(v)

    @field_validator("homepage", "source_url", "html_source_url")
    @classmethod
    def clean_url(cls, v):
        return _http_url(v)

    @model_validator(mode="after")
    def consistent(self):
        if self.repo_key != f"{self.owner}/{self.name}".lower():
            raise ValueError("repo_key не совпадает с owner/name")
        if self.created_at > self.collected_at + CLOCK_SKEW:
            raise ValueError("репозиторий создан позже момента сбора")
        if self.pushed_at and self.pushed_at < self.created_at:
            raise ValueError("pushed_at раньше created_at")
        has_html = any(v is not None for v in (self.releases_count, self.used_by, self.contributors,
                                                self.stars_html, self.primary_language_pct))
        if has_html and (self.html_collected_at is None or self.source != "github_api+github_html"):
            raise ValueError("есть поля из HTML, но не указано происхождение HTML (source/html_collected_at)")
        return self


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_key: str = Field(min_length=5, max_length=400)
    repo_key: str = Field(min_length=3, max_length=140)
    event_type: Literal["issue", "pull_request", "release"]
    github_id: int | None = Field(default=None, gt=0)
    number: int | None = Field(default=None, gt=0)
    tag: str | None = Field(default=None, min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=1024)
    state: Literal["open", "closed", "released", "prerelease"]
    state_reason: str | None = Field(default=None, pattern=r"^[a-z_]{3,30}$")
    is_draft: bool | None = None
    author_association: AuthorAssociation | None = None
    author_is_bot: bool | None = None
    labels: list[str] = Field(default_factory=list, max_length=100)
    label_color: str | None = Field(default=None, pattern=r"^[0-9a-f]{6}$")
    comments: int | None = Field(default=None, ge=0, le=100_000)
    reactions: int | None = Field(default=None, ge=0, le=1_000_000)
    created_at: datetime
    gh_updated_at: datetime | None = None
    closed_at: datetime | None = None
    merged_at: datetime | None = None
    commit_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")
    url: str
    source: Literal["github_api", "github_html"]
    source_url: str
    parser_version: str = Field(min_length=3)
    collected_at: datetime

    @field_validator("repo_key")
    @classmethod
    def slug_key(cls, v):
        """Собственный валидатор 4: бизнес-ключ репозитория — slug owner/name."""
        return _slug(v)

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, v):
        """Собственный валидатор 1: заголовок приводится к NFKC без невидимых символов."""
        if isinstance(v, str):
            v = " ".join(unicodedata.normalize("NFKC", v).translate(ZERO_WIDTH).split())
        return v

    @field_validator("created_at", "gh_updated_at", "closed_at", "merged_at", "collected_at")
    @classmethod
    def utc_only(cls, v):
        """Собственный валидатор 2: наивные даты запрещены, всё приводится к UTC."""
        return _as_utc(v)

    @field_validator("url", "source_url")
    @classmethod
    def clean_url(cls, v):
        """Собственный валидатор 3: http(s) и без параметров трекинга."""
        return _http_url(v)

    @model_validator(mode="after")
    def consistent(self):
        """Согласованность полей между собой."""
        if self.event_type == "release":
            if self.tag is None or self.number is not None:
                raise ValueError("релиз должен иметь tag и не иметь number")
            if self.event_key != f"{self.repo_key}@{self.tag}":
                raise ValueError("event_key релиза должен быть repo_key@tag")
            if self.state not in ("released", "prerelease"):
                raise ValueError("состояние релиза — released или prerelease")
        else:
            if self.number is None or self.tag is not None:
                raise ValueError("issue/PR должен иметь number и не иметь tag")
            if self.event_key != f"{self.repo_key}#{self.number}":
                raise ValueError("event_key issue/PR должен быть repo_key#number")
            if self.state not in ("open", "closed"):
                raise ValueError("состояние issue/PR — open или closed")
            if (self.state == "closed") != (self.closed_at is not None):
                raise ValueError("state='closed' должен сопровождаться closed_at, а open — его отсутствием")
        if self.closed_at and self.closed_at < self.created_at:
            raise ValueError("closed_at раньше created_at")
        if self.merged_at is not None:
            if self.event_type != "pull_request":
                raise ValueError("merged_at допустим только у pull request")
            if self.merged_at < self.created_at:
                raise ValueError("merged_at раньше created_at")
        if self.created_at > self.collected_at + CLOCK_SKEW:
            raise ValueError("событие создано позже момента сбора (дата из будущего)")
        return self


def first_error(exc: ValidationError) -> tuple[str, str]:
    err = exc.errors()[0]
    field = ".".join(str(p) for p in err["loc"]) or "<запись>"
    msg = err["msg"].replace("Value error, ", "")
    return field, msg


# ------------------------------------------------------------------ демонстрация
SAMPLE_EVENT = {   # опорная запись для проб, если чистый слой ещё не собран
    "event_key": "scrapy/scrapy#6123", "repo_key": "scrapy/scrapy", "event_type": "issue",
    "github_id": 2_000_000_001, "number": 6123, "tag": None, "title": "Example issue",
    "state": "closed", "state_reason": "completed", "is_draft": None, "author_association": "CONTRIBUTOR",
    "author_is_bot": False, "labels": ["bug"], "label_color": "d73a4a", "comments": 3, "reactions": 0,
    "created_at": datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
    "gh_updated_at": datetime(2026, 5, 3, 9, 0, tzinfo=timezone.utc),
    "closed_at": datetime(2026, 5, 3, 9, 0, tzinfo=timezone.utc), "merged_at": None, "commit_sha": None,
    "url": "https://github.com/scrapy/scrapy/issues/6123", "source": "github_api",
    "source_url": "https://api.github.com/repos/scrapy/scrapy/issues?state=all&page=1",
    "parser_version": "github-api/1.0", "collected_at": datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc),
}

EVENT_PROBES = [
    ("отрицательное число комментариев", {"comments": -1}),
    ("пустой заголовок (пробелы и zero-width)", {"title": " ​\xa0 "}),
    ("наивная дата без часового пояса", {"created_at": datetime(2026, 5, 1, 10, 0)}),
    ("state=closed без closed_at", {"state": "closed", "closed_at": None}),
    ("closed_at раньше created_at", lambda b: {"closed_at": b["created_at"] - timedelta(days=1)}),
    ("merged_at у issue (не PR)", lambda b: {"merged_at": b["closed_at"]}),
    ("event_key не равен repo_key#number", {"event_key": "scrapy/scrapy#1"}),
    ("цвет метки не hex-6", {"label_color": "#D73A4A"}),
    ("repo_key не в виде owner/name", {"repo_key": "Scrapy"}),
    ("неизвестный тип события", {"event_type": "commit"}),
    ("событие из будущего", lambda b: {"created_at": b["collected_at"] + timedelta(days=30),
                                       "closed_at": b["collected_at"] + timedelta(days=31)}),
    ("URL с параметрами трекинга", {"url": "https://github.com/scrapy/scrapy/issues/6123?utm_source=x"}),
]

REPO_PROBES = [
    ("отрицательное число звёзд", {"stars": -5}),
    ("доля основного языка 120 %", {"primary_language_pct": 120.0}),
    ("github_id = 0", {"github_id": 0}),
]


def run_probes(event_base: dict | None = None, repo_base: dict | None = None) -> list[dict]:
    """Таблица «проверка → результат → сообщение» на заведомо битых записях."""
    rows = []
    base = dict(event_base or SAMPLE_EVENT)
    # опорная запись должна быть закрытым issue — подгоняем ключевые поля, если взята из данных
    if base.get("event_type") != "issue" or base.get("state") != "closed":
        base = dict(SAMPLE_EVENT)
    for name, patch in EVENT_PROBES:
        patch = patch(base) if callable(patch) else patch     # часть проб строится от опорной записи
        rows.append(_probe(Event, "Event", name, {**base, **patch}))
    if repo_base:
        for name, patch in REPO_PROBES:
            rows.append(_probe(Repository, "Repository", name, {**repo_base, **patch}))
    return rows


def _probe(model, model_name: str, name: str, payload: dict) -> dict:
    try:
        model(**payload)
        return {"модель": model_name, "проверка": name, "результат": "ПРОПУЩЕНА (плохо)", "поле": "", "сообщение": ""}
    except ValidationError as exc:
        field, msg = first_error(exc)
        return {"модель": model_name, "проверка": name, "результат": "отклонена", "поле": field, "сообщение": msg}
