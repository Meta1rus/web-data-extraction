"""Проверка robots.txt по RFC 9309 (с шаблонами * и $) + сверка с urllib.robotparser.

Зачем свой разборщик: robots.txt GitHub почти целиком состоит из шаблонов
(`Disallow: /*/*/contributors`, `Disallow: /*tab=*` …), а поведение стандартного
urllib.robotparser зависит от версии Python: в 3.10–3.12 шаблоны не поддерживаются
(строка считается буквальным префиксом «/*/*/contributors» и запрещённые адреса
разрешаются), в свежих выпусках 3.13 поддержка появилась, но, например, адрес
с `?tab=` по-прежнему разрешается. Итоговое решение здесь консервативное:
адрес разрешён, только если его разрешают ОБА разборщика.
"""
from __future__ import annotations

import re
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[str, str]] = field(default_factory=list)   # (allow|disallow, pattern)
    crawl_delay: float | None = None


def _pattern_to_regex(pattern: str) -> re.Pattern:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile("^" + rx + ("$" if anchored else ""))


def _parse(text: str) -> list[_Group]:
    groups: list[_Group] = []
    current: _Group | None = None
    last_was_agent = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if current is None or not last_was_agent:
                current = _Group()
                groups.append(current)
            current.agents.append(value.lower())
            last_was_agent = True
            continue
        last_was_agent = False
        if current is None:
            continue
        if key in ("allow", "disallow"):
            if value:                         # пустой Disallow ничего не запрещает
                current.rules.append((key, value))
        elif key == "crawl-delay":
            try:
                current.crawl_delay = float(value)
            except ValueError:
                current.crawl_delay = None
    return groups


class RobotsPolicy:
    """Решение «можно / нельзя» для конкретного User-Agent."""

    def __init__(self, robots_url: str, status: int | None, text: str, user_agent: str):
        self.robots_url = robots_url
        self.status = status
        self.user_agent = user_agent
        self.token = user_agent.split("/", 1)[0].strip().lower()   # product token: psu-edu-crawler
        self.text = text or ""
        groups = _parse(self.text)
        own = [g for g in groups if any(a != "*" and a == self.token for a in g.agents)]
        star = [g for g in groups if "*" in g.agents]
        chosen = own or star
        self.group_name = "собственная группа" if own else ("User-agent: *" if star else "нет группы")
        self.rules = [r for g in chosen for r in g.rules]
        delays = [g.crawl_delay for g in chosen if g.crawl_delay is not None]
        self.crawl_delay = max(delays) if delays else None
        self._compiled = [(kind, pat, _pattern_to_regex(pat)) for kind, pat in self.rules]
        self._std = urllib.robotparser.RobotFileParser()
        self._std.parse(self.text.splitlines())

    # --- RFC 9309: 4xx → всё разрешено, 5xx/недоступен → всё запрещено
    def _status_decision(self) -> tuple[bool, str] | None:
        if self.status is None or self.status >= 500:
            return False, f"robots.txt недоступен (статус {self.status}) → по RFC 9309 обход запрещён"
        if 400 <= self.status < 500:
            return True, f"robots.txt отсутствует (статус {self.status}) → по RFC 9309 ограничений нет"
        return None

    def check(self, url: str) -> dict:
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        by_status = self._status_decision()
        if by_status is not None:
            allowed, reason = by_status
            return {"url": url, "path": path, "rfc9309": allowed, "urllib": None,
                    "allowed": allowed, "rule": None, "reason": reason}
        best: tuple[int, str, str] | None = None             # (длина шаблона, тип, шаблон)
        for kind, pat, rx in self._compiled:
            if rx.match(path):
                cand = (len(pat), kind, pat)
                # самое длинное совпадение; при равной длине побеждает allow
                if best is None or cand[0] > best[0] or (cand[0] == best[0] and kind == "allow"):
                    best = cand
        rfc_allowed = True if best is None else best[1] == "allow"
        std_allowed = self._std.can_fetch(self.user_agent, url)
        allowed = rfc_allowed and std_allowed
        if best is None:
            reason = "ни одно правило не совпало"
        else:
            reason = f"{best[1].capitalize()}: {best[2]}"
        if rfc_allowed != std_allowed:
            reason += f" (urllib.robotparser считает: {'можно' if std_allowed else 'нельзя'} — расхождение)"
        return {"url": url, "path": path, "rfc9309": rfc_allowed, "urllib": std_allowed,
                "allowed": allowed, "rule": best[2] if best else None, "reason": reason}
