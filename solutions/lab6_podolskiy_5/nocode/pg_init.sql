-- Postgres для no-code части (n8n). Выполняется автоматически при первом старте контейнера
-- (docker-entrypoint-initdb.d) или вручную: psql -h localhost -U lab6 -d lab6 -f nocode/pg_init.sql

CREATE TABLE IF NOT EXISTS events_n8n (
    event_key     TEXT PRIMARY KEY,                       -- бизнес-ключ owner/repo#номер (матчинг Upsert)
    repo_key      TEXT NOT NULL,
    number        INTEGER CHECK (number > 0),
    event_type    TEXT NOT NULL CHECK (event_type IN ('issue', 'pull_request')),
    title         TEXT NOT NULL CHECK (length(btrim(title)) > 0),
    state         TEXT NOT NULL CHECK (state IN ('open', 'closed')),
    created_at    TIMESTAMPTZ NOT NULL,
    closed_at     TIMESTAMPTZ,
    comments      INTEGER CHECK (comments >= 0),
    label_color   TEXT CHECK (label_color IS NULL OR label_color ~ '^[0-9a-f]{6}$'),   -- TEXT: ведущие нули сохраняются
    url           TEXT NOT NULL,
    is_synthetic  BOOLEAN NOT NULL DEFAULT FALSE,
    source        TEXT NOT NULL,
    collected_at  TIMESTAMPTZ NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((state = 'closed') = (closed_at IS NOT NULL)),
    CHECK (closed_at IS NULL OR closed_at >= created_at)
);

CREATE TABLE IF NOT EXISTS rejects_n8n (
    event_key     TEXT NOT NULL,
    reason        TEXT NOT NULL,
    payload       TEXT NOT NULL,
    is_synthetic  BOOLEAN NOT NULL DEFAULT FALSE,
    rejected_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_key, reason)                       -- Upsert узла n8n матчится по этой паре
);

-- История в духе SCD2: то, чего узел Postgres сделать не может, делает триггер БД
CREATE TABLE IF NOT EXISTS events_n8n_history (
    event_key   TEXT NOT NULL,
    valid_from  TIMESTAMPTZ NOT NULL,
    valid_to    TIMESTAMPTZ,
    state       TEXT,
    closed_at   TIMESTAMPTZ,
    comments    INTEGER,
    title       TEXT,
    PRIMARY KEY (event_key, valid_from)
);

-- Узел n8n выполняет INSERT … ON CONFLICT DO UPDATE SET <все поля> без WHERE, то есть «обновляет»
-- строку при каждом запуске. Триггер отменяет такое обновление, если бизнес-поля не изменились
-- (BEFORE UPDATE → RETURN NULL): updated_at остаётся прежним, а история пишется только при реальном изменении.
CREATE OR REPLACE FUNCTION events_n8n_upsert_guard() RETURNS trigger AS $$
BEGIN
    IF (NEW.title, NEW.state, NEW.closed_at, NEW.comments, NEW.label_color, NEW.created_at, NEW.event_type)
       IS NOT DISTINCT FROM
       (OLD.title, OLD.state, OLD.closed_at, OLD.comments, OLD.label_color, OLD.created_at, OLD.event_type) THEN
        RETURN NULL;
    END IF;
    NEW.updated_at := now();
    UPDATE events_n8n_history SET valid_to = now() WHERE event_key = OLD.event_key AND valid_to IS NULL;
    INSERT INTO events_n8n_history VALUES (NEW.event_key, now(), NULL, NEW.state, NEW.closed_at, NEW.comments, NEW.title);
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION events_n8n_first_version() RETURNS trigger AS $$
BEGIN
    INSERT INTO events_n8n_history VALUES (NEW.event_key, now(), NULL, NEW.state, NEW.closed_at, NEW.comments, NEW.title);
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_events_n8n_guard ON events_n8n;
CREATE TRIGGER trg_events_n8n_guard BEFORE UPDATE ON events_n8n
    FOR EACH ROW EXECUTE FUNCTION events_n8n_upsert_guard();
DROP TRIGGER IF EXISTS trg_events_n8n_first ON events_n8n;
CREATE TRIGGER trg_events_n8n_first AFTER INSERT ON events_n8n
    FOR EACH ROW EXECUTE FUNCTION events_n8n_first_version();

CREATE INDEX IF NOT EXISTS ix_events_n8n_created ON events_n8n (created_at);
