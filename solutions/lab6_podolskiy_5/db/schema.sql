-- Хранилище варианта 5 «Репозитории и активность»: репозиторий (1) → события (N).
-- СУБД: SQLite ≥ 3.35 (UPSERT, частичные индексы, json_valid).
--
-- Соглашение о датах: TEXT ISO 8601 в UTC с суффиксом Z — 'YYYY-MM-DDTHH:MM:SSZ'.
-- CHECK вида  col IS strftime('%Y-%m-%dT%H:%M:%SZ', col)  отвергает наивные даты,
-- смещения (+03:00), несуществующие даты (2024-02-31) и мусор; NULL допускается
-- только там, где столбец не объявлен NOT NULL. Единый формат делает строковое
-- сравнение хронологическим (closed_at >= created_at).
-- Служебные моменты загрузки (updated_at, valid_from/valid_to) — с миллисекундами.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- справочник репозиториев
CREATE TABLE IF NOT EXISTS repositories (
    repo_key             TEXT PRIMARY KEY                    -- бизнес-ключ «owner/name» (не URL)
                         CHECK (repo_key = lower(repo_key) AND repo_key GLOB '?*/?*' AND repo_key NOT GLOB '*/*/*'),
    github_id            INTEGER NOT NULL UNIQUE CHECK (github_id > 0),   -- стабилен при переименовании
    owner                TEXT NOT NULL CHECK (length(owner) BETWEEN 1 AND 39),
    name                 TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    description          TEXT,
    homepage             TEXT CHECK (homepage IS NULL OR homepage GLOB 'http*://*'),
    primary_language     TEXT,
    license_spdx         TEXT,
    topics               TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(topics) AND json_type(topics) = 'array'),
    stars                INTEGER NOT NULL CHECK (stars >= 0),
    forks                INTEGER NOT NULL CHECK (forks >= 0),
    watchers             INTEGER CHECK (watchers IS NULL OR watchers >= 0),
    open_issues_prs      INTEGER NOT NULL CHECK (open_issues_prs >= 0),
    size_kb              INTEGER CHECK (size_kb IS NULL OR size_kb >= 0),
    default_branch       TEXT,
    archived             INTEGER NOT NULL CHECK (archived IN (0, 1)),
    created_at           TEXT NOT NULL CHECK (created_at IS strftime('%Y-%m-%dT%H:%M:%SZ', created_at)),
    gh_updated_at        TEXT CHECK (gh_updated_at IS strftime('%Y-%m-%dT%H:%M:%SZ', gh_updated_at)),
    pushed_at            TEXT CHECK (pushed_at IS strftime('%Y-%m-%dT%H:%M:%SZ', pushed_at)),
    -- поля со страницы github.com (в API их нет или они требуют отдельных запросов)
    releases_count       INTEGER CHECK (releases_count IS NULL OR releases_count >= 0),
    latest_release_tag   TEXT,
    latest_release_at    TEXT CHECK (latest_release_at IS strftime('%Y-%m-%dT%H:%M:%SZ', latest_release_at)),
    used_by              INTEGER CHECK (used_by IS NULL OR used_by >= 0),
    contributors         INTEGER CHECK (contributors IS NULL OR contributors >= 0),
    contributors_is_lower_bound INTEGER CHECK (contributors_is_lower_bound IS NULL OR contributors_is_lower_bound IN (0, 1)),
    primary_language_pct REAL CHECK (primary_language_pct IS NULL OR primary_language_pct BETWEEN 0 AND 100),
    languages            TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(languages) AND json_type(languages) = 'array'),
    stars_html           INTEGER CHECK (stars_html IS NULL OR stars_html >= 0),
    html_collected_at    TEXT CHECK (html_collected_at IS strftime('%Y-%m-%dT%H:%M:%SZ', html_collected_at)),
    html_source_url      TEXT,
    -- происхождение и служебные поля
    source               TEXT NOT NULL CHECK (source IN ('github_api', 'github_api+github_html')),
    source_url           TEXT NOT NULL CHECK (source_url GLOB 'http*://*'),
    parser_version       TEXT NOT NULL,
    collected_at         TEXT NOT NULL CHECK (collected_at IS strftime('%Y-%m-%dT%H:%M:%SZ', collected_at)),
    updated_at           TEXT NOT NULL CHECK (updated_at IS strftime('%Y-%m-%dT%H:%M:%fZ', updated_at)),
    row_hash             TEXT NOT NULL CHECK (length(row_hash) = 64),
    CHECK (pushed_at IS NULL OR pushed_at >= created_at),
    CHECK ((html_collected_at IS NULL) = (source = 'github_api'))
);
CREATE INDEX IF NOT EXISTS ix_repos_stars ON repositories (stars DESC);          -- топ-N по звёздам

-- ---------------------------------------------------------------- события (issue / PR / release)
CREATE TABLE IF NOT EXISTS events (
    event_key            TEXT PRIMARY KEY,                  -- «owner/name#номер» или «owner/name@тег»
    repo_key             TEXT NOT NULL REFERENCES repositories (repo_key) ON UPDATE CASCADE ON DELETE RESTRICT,
    event_type           TEXT NOT NULL CHECK (event_type IN ('issue', 'pull_request', 'release')),
    github_id            INTEGER UNIQUE CHECK (github_id IS NULL OR github_id > 0),
    number               INTEGER CHECK (number IS NULL OR number > 0),
    tag                  TEXT CHECK (tag IS NULL OR length(tag) > 0),
    title                TEXT NOT NULL CHECK (length(trim(title)) > 0),
    state                TEXT NOT NULL CHECK (state IN ('open', 'closed', 'released', 'prerelease')),
    state_reason         TEXT,
    is_draft             INTEGER CHECK (is_draft IS NULL OR is_draft IN (0, 1)),
    author_association   TEXT,
    author_is_bot        INTEGER CHECK (author_is_bot IS NULL OR author_is_bot IN (0, 1)),
    labels               TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(labels) AND json_type(labels) = 'array'),
    label_color          TEXT CHECK (label_color IS NULL OR (length(label_color) = 6 AND label_color NOT GLOB '*[^0-9a-f]*')),
    comments             INTEGER CHECK (comments IS NULL OR comments >= 0),
    reactions            INTEGER CHECK (reactions IS NULL OR reactions >= 0),
    created_at           TEXT NOT NULL CHECK (created_at IS strftime('%Y-%m-%dT%H:%M:%SZ', created_at)),
    gh_updated_at        TEXT CHECK (gh_updated_at IS strftime('%Y-%m-%dT%H:%M:%SZ', gh_updated_at)),
    closed_at            TEXT CHECK (closed_at IS strftime('%Y-%m-%dT%H:%M:%SZ', closed_at)),
    merged_at            TEXT CHECK (merged_at IS strftime('%Y-%m-%dT%H:%M:%SZ', merged_at)),
    commit_sha           TEXT CHECK (commit_sha IS NULL OR (length(commit_sha) BETWEEN 7 AND 40 AND commit_sha NOT GLOB '*[^0-9a-f]*')),
    url                  TEXT NOT NULL CHECK (url GLOB 'http*://*'),
    source               TEXT NOT NULL CHECK (source IN ('github_api', 'github_html')),
    source_url           TEXT NOT NULL CHECK (source_url GLOB 'http*://*'),
    parser_version       TEXT NOT NULL,
    collected_at         TEXT NOT NULL CHECK (collected_at IS strftime('%Y-%m-%dT%H:%M:%SZ', collected_at)),
    updated_at           TEXT NOT NULL CHECK (updated_at IS strftime('%Y-%m-%dT%H:%M:%fZ', updated_at)),
    row_hash             TEXT NOT NULL CHECK (length(row_hash) = 64),
    -- согласованность полей (дублирует контракт — второй рубеж на уровне набора)
    CHECK ((event_type = 'release') = (tag IS NOT NULL)),
    CHECK ((event_type = 'release') = (number IS NULL)),
    CHECK (event_key = repo_key || CASE WHEN event_type = 'release' THEN '@' || tag ELSE '#' || number END),
    CHECK (event_type = 'release' OR state IN ('open', 'closed')),
    CHECK (event_type = 'release' OR (state = 'closed') = (closed_at IS NOT NULL)),
    CHECK (closed_at IS NULL OR closed_at >= created_at),
    CHECK (merged_at IS NULL OR (event_type = 'pull_request' AND merged_at >= created_at))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_repo_number ON events (repo_key, number) WHERE number IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_repo_tag ON events (repo_key, tag) WHERE tag IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_events_repo_created ON events (repo_key, created_at);  -- активность репозитория за период
CREATE INDEX IF NOT EXISTS ix_events_created ON events (created_at);                 -- динамика по неделям
CREATE INDEX IF NOT EXISTS ix_events_type_state ON events (event_type, state);       -- доли открытых / закрытых

-- ---------------------------------------------------------------- история изменений (SCD Type 2)
-- valid_from — момент загрузки, с которого версия действует; valid_to — момент, когда её сменила
-- следующая (NULL = актуальная). Версии создаются только при смене row_hash.
CREATE TABLE IF NOT EXISTS repositories_history (
    repo_key             TEXT NOT NULL,
    valid_from           TEXT NOT NULL CHECK (valid_from IS strftime('%Y-%m-%dT%H:%M:%fZ', valid_from)),
    valid_to             TEXT CHECK (valid_to IS strftime('%Y-%m-%dT%H:%M:%fZ', valid_to)),
    is_current           INTEGER NOT NULL CHECK (is_current IN (0, 1)),
    stars                INTEGER,
    forks                INTEGER,
    watchers             INTEGER,
    open_issues_prs      INTEGER,
    releases_count       INTEGER,
    latest_release_tag   TEXT,
    used_by              INTEGER,
    contributors         INTEGER,
    archived             INTEGER,
    description          TEXT,
    collected_at         TEXT NOT NULL,
    row_hash             TEXT NOT NULL,
    load_id              TEXT NOT NULL,
    PRIMARY KEY (repo_key, valid_from),
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    CHECK ((valid_to IS NULL) = (is_current = 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_repo_hist_current ON repositories_history (repo_key) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS ix_repo_hist_asof ON repositories_history (valid_from, valid_to);

CREATE TABLE IF NOT EXISTS events_history (
    event_key            TEXT NOT NULL,
    valid_from           TEXT NOT NULL CHECK (valid_from IS strftime('%Y-%m-%dT%H:%M:%fZ', valid_from)),
    valid_to             TEXT CHECK (valid_to IS strftime('%Y-%m-%dT%H:%M:%fZ', valid_to)),
    is_current           INTEGER NOT NULL CHECK (is_current IN (0, 1)),
    repo_key             TEXT NOT NULL,
    event_type           TEXT NOT NULL,
    state                TEXT,
    state_reason         TEXT,
    title                TEXT,
    labels               TEXT,
    comments             INTEGER,
    closed_at            TEXT,
    merged_at            TEXT,
    gh_updated_at        TEXT,
    collected_at         TEXT NOT NULL,
    row_hash             TEXT NOT NULL,
    load_id              TEXT NOT NULL,
    PRIMARY KEY (event_key, valid_from),
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    CHECK ((valid_to IS NULL) = (is_current = 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_event_hist_current ON events_history (event_key) WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS ix_event_hist_asof ON events_history (valid_from, valid_to);

-- ---------------------------------------------------------------- отбраковка и журнал загрузок
CREATE TABLE IF NOT EXISTS rejects (
    reject_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity               TEXT NOT NULL CHECK (entity IN ('repository', 'repo_page', 'event', 'raw')),
    business_key         TEXT,
    stage                TEXT NOT NULL,
    field                TEXT,
    reason               TEXT NOT NULL,
    is_synthetic         INTEGER NOT NULL CHECK (is_synthetic IN (0, 1)),
    payload              TEXT NOT NULL CHECK (json_valid(payload)),
    payload_hash         TEXT NOT NULL,
    source               TEXT,
    source_url           TEXT,
    collected_at         TEXT,
    first_seen_at        TEXT NOT NULL,
    load_id              TEXT NOT NULL,
    UNIQUE (entity, payload_hash, reason)              -- повторная загрузка не плодит дубли отбраковки
);
CREATE INDEX IF NOT EXISTS ix_rejects_stage_reason ON rejects (stage, reason);

CREATE TABLE IF NOT EXISTS load_runs (
    load_id              TEXT PRIMARY KEY,
    label                TEXT,
    started_at           TEXT NOT NULL,
    finished_at          TEXT,
    repos_in             INTEGER, repos_inserted INTEGER, repos_updated INTEGER, repos_unchanged INTEGER,
    events_in            INTEGER, events_inserted INTEGER, events_updated INTEGER, events_unchanged INTEGER,
    history_versions_added INTEGER,
    rejects_added        INTEGER
);

-- ---------------------------------------------------------------- представления для слоя mart
CREATE VIEW IF NOT EXISTS v_repo_activity AS               -- агрегаты по репозиториям (категориям)
SELECT r.repo_key,
       r.primary_language,
       r.stars,
       COUNT(e.event_key)                                                        AS events_total,
       SUM(e.event_type = 'issue')                                               AS issues,
       SUM(e.event_type = 'pull_request')                                        AS pull_requests,
       SUM(e.event_type = 'release')                                             AS releases_seen,
       SUM(e.event_type = 'issue' AND e.state = 'open')                          AS issues_open,
       SUM(e.event_type = 'pull_request' AND e.merged_at IS NOT NULL)            AS prs_merged,
       ROUND(100.0 * SUM(e.event_type = 'pull_request' AND e.merged_at IS NOT NULL)
             / NULLIF(SUM(e.event_type = 'pull_request' AND e.state = 'closed'), 0), 1) AS pr_merge_rate_pct,
       ROUND(AVG(CASE WHEN e.closed_at IS NOT NULL
                      THEN (julianday(e.closed_at) - julianday(e.created_at)) * 24 END), 1) AS avg_hours_to_close,
       SUM(e.author_is_bot = 1)                                                  AS bot_events,
       MAX(e.created_at)                                                         AS last_event_at
FROM repositories r
LEFT JOIN events e ON e.repo_key = r.repo_key
GROUP BY r.repo_key, r.primary_language, r.stars;

CREATE VIEW IF NOT EXISTS v_weekly_activity AS             -- динамика по неделям (UTC, неделя с понедельника)
SELECT date(e.created_at, 'weekday 0', '-6 days')          AS week_start,
       e.event_type,
       COUNT(*)                                            AS created,
       SUM(e.closed_at IS NOT NULL)                        AS closed_later
FROM events e
GROUP BY week_start, e.event_type;

CREATE VIEW IF NOT EXISTS v_top_repos AS                   -- топ-N: популярность против нагрузки
SELECT r.repo_key,
       r.stars,
       r.used_by,
       r.contributors,
       r.releases_count,
       r.open_issues_prs,
       ROUND(1000.0 * r.open_issues_prs / NULLIF(r.stars, 0), 2) AS open_items_per_1k_stars,
       RANK() OVER (ORDER BY r.stars DESC)                 AS rank_by_stars,
       RANK() OVER (ORDER BY r.used_by DESC)               AS rank_by_used_by
FROM repositories r;
