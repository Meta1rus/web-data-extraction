"""Общее состояние прогона: журнал, ограничитель, предохранители, политика повторов,
хранилища ETag и «водяного знака». Один объект на crawler; им пользуются middleware,
расширение RunObserver и паук.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from src.breaker import BreakerRegistry
from src.limiter import DomainLimiter, SharedTokenBucket
from src.observability import JsonLogger
from src.retry import RetryPolicy

ROOT = Path(__file__).resolve().parents[2]


def host_of(url: str) -> str:
    return urlsplit(url).netloc


def _load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)              # атомарная замена: прерванный прогон не портит состояние


class Runtime:
    def __init__(self, crawler):
        cfg = dict(crawler.settings.getdict("LAB7"))
        self.crawler = crawler
        self.cfg = cfg
        self.run_id = cfg.get("run_id") or datetime.now(timezone.utc).strftime("run_%Y%m%dT%H%M%SZ")
        self.label = cfg.get("label", "run")
        self.mode = cfg.get("mode", "B")
        self.logger = JsonLogger(cfg.get("log_path") or ROOT / "logs" / f"run_{self.run_id}.jsonl", self.run_id)

        lim = cfg.get("limiter", {})
        self.limiter = DomainLimiter(
            default_rate=lim.get("rate", 2.0), default_burst=lim.get("burst", 2), jitter=lim.get("jitter", 0.25),
            adaptive=lim.get("adaptive", True), per_domain=lim.get("per_domain", {}),
        ) if lim.get("enabled", True) else None
        self.shared_dir = Path(lim["shared_dir"]) if lim.get("shared_dir") else None
        self.shared: dict[str, SharedTokenBucket] = {}

        rcfg = cfg.get("retry", {})
        self.retry = RetryPolicy(**rcfg.get("params", {})) if rcfg.get("enabled", True) else None

        bcfg = cfg.get("breaker", {})
        self.breakers = BreakerRegistry(
            fail_threshold=bcfg.get("fail_threshold", 3), reset_after=bcfg.get("reset_after", 10.0),
            max_reset=bcfg.get("max_reset", 120.0), on_transition=self._on_breaker,
        ) if bcfg.get("enabled", True) else None

        dcfg = cfg.get("detector", {})
        self.detector_enabled = dcfg.get("enabled", True)
        self.min_bytes = int(dcfg.get("min_bytes", 1500))
        self.incidents_dir = Path(cfg.get("incidents_dir", ROOT / "logs" / "incidents"))

        ccfg = cfg.get("conditional", {})
        self.cond_send = bool(ccfg.get("send", False))
        self.cond_record = bool(ccfg.get("record", True))
        self.etags_path = Path(ccfg.get("path", ROOT / "state" / "etags.json"))
        self.etags: dict = _load_json(self.etags_path, {})

        icfg = cfg.get("incremental", {})
        self.incremental = bool(icfg.get("enabled", False))
        self.revalidate_after_s = float(icfg.get("revalidate_after_s", 86400))
        self.watermark_path = Path(icfg.get("path", ROOT / "state" / "watermark.json"))
        self.watermark: dict = _load_json(self.watermark_path, {"pages": {}, "products": {}})
        self.watermark.setdefault("pages", {})
        self.watermark.setdefault("products", {})

        self.deferred_path = Path(cfg.get("deferred_path", ROOT / "state" / "deferred.json"))
        self.max_requests = cfg.get("max_requests")
        self.start_hosts = set(cfg.get("start_hosts", []))
        self.sent = 0
        self.pending: dict[int, object] = {}       # запросы, возвращаемые в очередь по таймеру
        self.closed = False
        self.blocked: dict[str, str] = {}
        self.deferred: list[dict] = []
        self.incident_seq = 0
        self.consecutive_403: dict[str, int] = {}

    # ---------------------------------------------------------------- helpers
    def log(self, event: str, **fields) -> dict:
        return self.logger.log(event, **fields)

    def _on_breaker(self, host: str, old: str, new: str, reason: str) -> None:
        self.log("breaker", host=host, from_state=old, to_state=new, reason=reason)

    def shared_bucket(self, host: str) -> SharedTokenBucket | None:
        if not self.shared_dir or not self.limiter:
            return None
        if host not in self.shared:
            pol = self.limiter.policy(host)
            safe = host.replace(":", "_")
            self.shared[host] = SharedTokenBucket(self.shared_dir / f"{safe}.json", pol.rate, pol.burst)
        return self.shared[host]

    def defer(self, request, reason: str) -> None:
        self.deferred.append({"url": request.url, "reason": reason, "kind": request.meta.get("page_kind"),
                              "page_no": (getattr(request, "cb_kwargs", None) or {}).get("page_no"),
                              "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})

    def save_state(self) -> None:
        if not self.cfg.get("write_state", True):      # «наивный» прогон A состояние не трогает
            return
        if self.cond_record:
            _save_json(self.etags_path, self.etags)
        if self.watermark.get("pages"):
            self.watermark["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.watermark["updated_by_run"] = self.run_id
            _save_json(self.watermark_path, self.watermark)
        _save_json(self.deferred_path, {"run_id": self.run_id, "deferred": self.deferred})

    @staticmethod
    def now() -> float:
        return time.monotonic()


def get_runtime(crawler) -> Runtime:
    rt = getattr(crawler, "_lab7_runtime", None)
    if rt is None:
        rt = Runtime(crawler)
        crawler._lab7_runtime = rt
    return rt
