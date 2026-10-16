"""Контракт данных чистого слоя (pydantic v2): типы, обязательность, диапазоны и собственные валидаторы.

Две связанные сущности:
  * TopicListing — карточка репозитория на странице темы (HTML), ключ (topic, repo_key);
  * RepoRecord   — метрики репозитория из GitHub REST API, ключ repo_key = owner/repo в нижнем регистре.
Отбракованные записи не теряются: validate_records возвращает их с причиной, полем и исходными данными.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

OWNER_RX = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
REPO_RX = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
TOPIC_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")
CLOCK_SKEW = timedelta(minutes=10)


def _check_full_name(v: str) -> str:
    if v.count("/") != 1:
        raise ValueError("ожидается формат owner/repo")
    owner, repo = v.split("/")
    if not OWNER_RX.match(owner):
        raise ValueError(f"недопустимое имя владельца: {owner!r}")
    if not REPO_RX.match(repo) or repo in (".", ".."):
        raise ValueError(f"недопустимое имя репозитория: {repo!r}")
    return v


def _utc(v: datetime | None) -> datetime | None:
    if v is None:
        return None
    if v.tzinfo is None:
        raise ValueError("дата без часового пояса недопустима (ожидается UTC)")
    return v.astimezone(timezone.utc)


def _topic_list(v: list[str]) -> list[str]:
    out: list[str] = []
    for t in v or []:
        t = (t or "").strip().lower()
        if not TOPIC_RX.match(t):
            raise ValueError(f"недопустимая тема {t!r}: только a-z, 0-9 и дефис, до 50 символов")
        if t not in out:
            out.append(t)
    return out


class TopicListing(BaseModel):
    """Карточка репозитория на странице темы GitHub."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    topic: str
    repo_key: str = Field(min_length=3, max_length=140)
    full_name: str
    page: int = Field(ge=1, le=100)
    rank_in_topic: int = Field(ge=1, le=10_000)
    stars_html: int | None = Field(default=None, ge=0, le=5_000_000)
    stars_html_approx: bool
    language_html: str | None = Field(default=None, max_length=60)
    description_html: str | None = Field(default=None, max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=20)     # GitHub допускает не более 20 тем
    html_updated_at: datetime | None = None
    collected_at: datetime
    source: Literal["github_topics_html"]
    parser_version: str = Field(min_length=1)
    run_id: str = Field(min_length=1)

    # --- собственные валидаторы
    @field_validator("topic")
    @classmethod
    def topic_slug(cls, v: str) -> str:
        v = v.lower()
        if not TOPIC_RX.match(v):
            raise ValueError(f"недопустимый slug темы: {v!r}")
        return v

    @field_validator("full_name")
    @classmethod
    def full_name_format(cls, v: str) -> str:
        return _check_full_name(v)

    @field_validator("tags")
    @classmethod
    def tags_are_topics(cls, v: list[str]) -> list[str]:
        return _topic_list(v)

    @field_validator("html_updated_at", "collected_at")
    @classmethod
    def tz_aware(cls, v):
        return _utc(v)

    @model_validator(mode="after")
    def consistency(self) -> "TopicListing":
        if self.repo_key != self.full_name.lower():
            raise ValueError("repo_key должен совпадать с full_name в нижнем регистре")
        if self.html_updated_at and self.html_updated_at > self.collected_at + CLOCK_SKEW:
            raise ValueError("дата обновления на странице позже момента сбора — ошибка разбора даты")
        # Наличие темы страницы среди тегов карточки НЕ требуем: страница темы может включать синонимы темы.
        return self


class RepoRecord(BaseModel):
    """Метрики репозитория из GitHub REST API (или Search API в режиме без токена)."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    repo_key: str = Field(min_length=3, max_length=140)
    repo_id: int = Field(gt=0)
    full_name: str
    owner_login: str = Field(min_length=1, max_length=39)
    owner_type: Literal["User", "Organization"] | None = None
    description: str | None = Field(default=None, max_length=2000)
    homepage: str | None = Field(default=None, max_length=500)
    language: str | None = Field(default=None, max_length=60)
    license_spdx: str | None = Field(default=None, max_length=64)
    topics: list[str] = Field(default_factory=list, max_length=20)
    stars: int = Field(ge=0, le=5_000_000)
    forks: int = Field(ge=0, le=5_000_000)
    open_issues: int = Field(ge=0, le=1_000_000)
    subscribers: int | None = Field(default=None, ge=0, le=1_000_000)
    size_kb: int = Field(ge=0, le=500_000_000)
    is_archived: bool
    is_fork: bool
    created_at: datetime
    pushed_at: datetime | None = None
    gh_updated_at: datetime
    default_branch: str | None = Field(default=None, max_length=255)
    collected_at: datetime
    source: Literal["github_rest_api", "github_search_api"]
    parser_version: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    etag: str | None = None

    # --- собственные валидаторы
    @field_validator("full_name")
    @classmethod
    def full_name_format(cls, v: str) -> str:
        return _check_full_name(v)

    @field_validator("topics")
    @classmethod
    def topics_are_slugs(cls, v: list[str]) -> list[str]:
        return _topic_list(v)

    @field_validator("license_spdx")
    @classmethod
    def license_normalized(cls, v: str | None) -> str | None:
        # NOASSERTION у GitHub означает «лицензия есть, но не распознана» — это не пропуск
        if v is None or v == "":
            return None
        if not re.fullmatch(r"[A-Za-z0-9.+-]{1,64}", v):
            raise ValueError(f"не похоже на SPDX-идентификатор: {v!r}")
        return v

    @field_validator("created_at", "pushed_at", "gh_updated_at", "collected_at")
    @classmethod
    def tz_aware(cls, v):
        return _utc(v)

    @model_validator(mode="after")
    def temporal_and_key_consistency(self) -> "RepoRecord":
        if self.repo_key != self.full_name.lower():
            raise ValueError("repo_key должен совпадать с full_name в нижнем регистре")
        if self.owner_login.lower() != self.repo_key.split("/")[0]:
            raise ValueError("owner_login не совпадает с владельцем в full_name")
        if self.created_at > self.collected_at + CLOCK_SKEW:
            raise ValueError("created_at позже момента сбора — ошибка часового пояса или разбора")
        if self.gh_updated_at < self.created_at - CLOCK_SKEW:
            raise ValueError("updated_at раньше created_at")
        # pushed_at раньше created_at допустим (импорт/перенос истории) — это проверяется метрикой, а не контрактом
        return self


def validate_records(records: list[dict], model: type[BaseModel], source: str) -> tuple[list[dict], list[dict]]:
    """Делит записи на валидные (нормализованные моделью) и отбракованные с причиной."""
    valid, rejects = [], []
    for rec in records:
        try:
            valid.append(model(**rec).model_dump())
        except ValidationError as e:
            errs = e.errors()
            first = errs[0]
            rejects.append({
                "source": source,
                "record_key": rec.get("repo_key") if model is RepoRecord else f"{rec.get('topic')}|{rec.get('repo_key')}",
                "field": ".".join(str(p) for p in first.get("loc", ())) or "__record__",
                "reason": first.get("msg", "ошибка валидации"),
                "errors": [{"field": ".".join(str(p) for p in er.get("loc", ())), "msg": er.get("msg")} for er in errs],
                "payload": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in rec.items()},
            })
    return valid, rejects
