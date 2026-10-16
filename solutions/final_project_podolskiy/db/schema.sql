-- Схема хранилища итогового проекта (SQLite 3.38+; JSON-функции встроены).
-- Правила: первичный ключ — бизнес-ключ (owner/repo в нижнем регистре), а не URL и не autoincrement;
-- NOT NULL на обязательных полях, CHECK на диапазоны; служебные поля source, collected_at, updated_at,
-- parser_version (+ first_seen_at, row_hash, run_id); индексы — под запросы слоя mart.
-- Время хранится строкой ISO 8601 в UTC с суффиксом Z: такие строки сравниваются корректно.

PRAGMA foreign_keys = ON;

-- Сущность 1: репозиторий (GitHub REST API) ------------------------------------------------------
CREATE TABLE IF NOT EXISTS repos (
    repo_key        TEXT PRIMARY KEY
                    CHECK (repo_key = lower(repo_key) AND instr(repo_key, '/') > 1),
    repo_id         INTEGER NOT NULL UNIQUE CHECK (repo_id > 0),
    full_name       TEXT NOT NULL,
    owner_login     TEXT NOT NULL,
    owner_type      TEXT CHECK (owner_type IS NULL OR owner_type IN ('User', 'Organization')),
    description     TEXT,
    homepage        TEXT,
    language        TEXT,
    license_spdx    TEXT,
    topics_json     TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(topics_json)),
    stars           INTEGER NOT NULL CHECK (stars >= 0),
    forks           INTEGER NOT NULL CHECK (forks >= 0),
    open_issues     INTEGER NOT NULL CHECK (open_issues >= 0),
    subscribers     INTEGER CHECK (subscribers IS NULL OR subscribers >= 0),
    size_kb         INTEGER NOT NULL CHECK (size_kb >= 0),
    is_archived     INTEGER NOT NULL CHECK (is_archived IN (0, 1)),
    is_fork         INTEGER NOT NULL CHECK (is_fork IN (0, 1)),
    created_at      TEXT NOT NULL,
    pushed_at       TEXT,
    gh_updated_at   TEXT NOT NULL,
    default_branch  TEXT,
    etag            TEXT,
    row_hash        TEXT NOT NULL,                       -- хэш бизнес-полей: updated_at меняется только при реальном изменении
    source          TEXT NOT NULL CHECK (source IN ('github_rest_api', 'github_search_api')),
    collected_at    TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    parser_version  TEXT NOT NULL,
    run_id          TEXT NOT NULL,
    CHECK (created_at <= collected_at)
);
CREATE INDEX IF NOT EXISTS idx_repos_language ON repos(language);
CREATE INDEX IF NOT EXISTS idx_repos_stars    ON repos(stars DESC);
CREATE INDEX IF NOT EXISTS idx_repos_pushed   ON repos(pushed_at);
CREATE INDEX IF NOT EXISTS idx_repos_size     ON repos(size_kb);

-- Сущность 2: карточка репозитория на странице темы (HTML) ----------------------------------------
-- Без внешнего ключа на repos: карточка может появиться раньше обогащения (API недоступен, квота).
-- Связь проверяется метрикой качества join_coverage_pct и выполняется в представлениях mart.
CREATE TABLE IF NOT EXISTS topic_listings (
    topic             TEXT NOT NULL CHECK (topic = lower(topic) AND length(topic) BETWEEN 1 AND 50),
    repo_key          TEXT NOT NULL CHECK (repo_key = lower(repo_key) AND instr(repo_key, '/') > 1),
    full_name         TEXT NOT NULL,
    page              INTEGER NOT NULL CHECK (page BETWEEN 1 AND 100),
    rank_in_topic     INTEGER NOT NULL CHECK (rank_in_topic >= 1),
    stars_html        INTEGER CHECK (stars_html IS NULL OR stars_html >= 0),
    stars_html_approx INTEGER NOT NULL CHECK (stars_html_approx IN (0, 1)),
    language_html     TEXT,
    description_html  TEXT,
    tags_json         TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(tags_json)),
    html_updated_at   TEXT,
    row_hash          TEXT NOT NULL,
    source            TEXT NOT NULL CHECK (source = 'github_topics_html'),
    collected_at      TEXT NOT NULL,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    parser_version    TEXT NOT NULL,
    run_id            TEXT NOT NULL,
    PRIMARY KEY (topic, repo_key),
    CHECK (first_seen_at <= last_seen_at)
);
CREATE INDEX IF NOT EXISTS idx_listings_repo ON topic_listings(repo_key);
CREATE INDEX IF NOT EXISTS idx_listings_seen ON topic_listings(last_seen_at);

-- Синонимы ключа (переименование/перенос репозитория: старый owner/repo -> текущий) ---------------
CREATE TABLE IF NOT EXISTS repo_aliases (
    alias_key   TEXT PRIMARY KEY CHECK (alias_key = lower(alias_key)),
    repo_key    TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- История (снапшоты): одна строка на репозиторий и дату проверки — для динамики за период сбора ---
CREATE TABLE IF NOT EXISTS repo_snapshots (
    repo_key       TEXT NOT NULL,
    snapshot_date  TEXT NOT NULL CHECK (length(snapshot_date) = 10),   -- YYYY-MM-DD (UTC)
    stars          INTEGER NOT NULL CHECK (stars >= 0),
    forks          INTEGER NOT NULL CHECK (forks >= 0),
    open_issues    INTEGER NOT NULL CHECK (open_issues >= 0),
    subscribers    INTEGER,
    size_kb        INTEGER NOT NULL CHECK (size_kb >= 0),
    pushed_at      TEXT,
    verified_by    TEXT NOT NULL CHECK (verified_by IN ('200', '304', 'search')),
    run_id         TEXT NOT NULL,
    PRIMARY KEY (repo_key, snapshot_date)
);

-- Отбраковка: каждая запись с причиной (материал для исправления парсера) -------------------------
CREATE TABLE IF NOT EXISTS rejects (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    source        TEXT NOT NULL,
    record_key    TEXT,
    field         TEXT NOT NULL,
    reason        TEXT NOT NULL,
    payload_json  TEXT NOT NULL CHECK (json_valid(payload_json)),
    seen_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rejects_run ON rejects(run_id);

-- Журнал прогонов: сравнение с прошлым прогоном, «нет изменений N прогонов подряд», возобновление --
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL CHECK (status IN ('running', 'finished', 'interrupted', 'failed')),
    verdict         TEXT CHECK (verdict IS NULL OR verdict IN ('green', 'yellow', 'red')),
    exit_code       INTEGER,
    resumed_from    TEXT,
    html_cards      INTEGER,
    api_200         INTEGER,
    api_304         INTEGER,
    clean_repos     INTEGER,
    clean_listings  INTEGER,
    rejects         INTEGER,
    inserted        INTEGER,
    updated         INTEGER,
    unchanged       INTEGER,
    mart_built      INTEGER CHECK (mart_built IS NULL OR mart_built IN (0, 1)),
    html_base       TEXT NOT NULL,
    api_base        TEXT NOT NULL,
    report_path     TEXT
);

-- Представления слоя mart ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_repo_topic;
CREATE VIEW v_repo_topic AS
SELECT l.topic,
       l.rank_in_topic,
       l.stars_html,
       l.last_seen_at,
       r.repo_key, r.full_name, r.owner_type, r.language, r.license_spdx,
       r.stars, r.forks, r.open_issues, r.subscribers, r.size_kb,
       r.is_archived, r.is_fork, r.created_at, r.pushed_at, r.gh_updated_at,
       r.collected_at AS api_collected_at,
       json_array_length(r.topics_json) AS n_topics
FROM topic_listings AS l
LEFT JOIN repo_aliases AS a ON a.alias_key = l.repo_key
JOIN repos AS r ON r.repo_key = COALESCE(a.repo_key, l.repo_key);

DROP VIEW IF EXISTS v_unenriched_listings;
CREATE VIEW v_unenriched_listings AS
SELECT l.topic, l.repo_key, l.last_seen_at
FROM topic_listings AS l
LEFT JOIN repo_aliases AS a ON a.alias_key = l.repo_key
LEFT JOIN repos AS r ON r.repo_key = COALESCE(a.repo_key, l.repo_key)
WHERE r.repo_key IS NULL;
