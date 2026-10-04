-- 0001_initial: esquema inicial de conteos anónimos, alertas de cola, sedes y salud.
-- RGPD: aquí NUNCA se guardan imágenes, vídeo, identificadores de personas ni datos de
-- trabajadores. Solo agregados anónimos por minuto y eventos de ocupación.
-- Todas las fechas en UTC (timestamptz). «minute» siempre truncado al minuto.

CREATE TABLE sites (
    site_id     text PRIMARY KEY CHECK (site_id ~ '^[a-z0-9][a-z0-9-]{2,39}$'),
    name        text NOT NULL,
    code        text NOT NULL DEFAULT '',
    timezone    text NOT NULL DEFAULT 'Europe/Madrid',
    active      boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Nombres de cámaras por sede (para informes y panel central). Sin URLs ni credenciales.
CREATE TABLE site_cameras (
    site_id     text NOT NULL REFERENCES sites (site_id) ON DELETE CASCADE,
    camera_id   text NOT NULL,
    name        text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, camera_id)
);

-- Copia de las reglas (línea de puerta / zona de cola) para interpretar los conteos.
CREATE TABLE analytics_rules (
    site_id     text NOT NULL REFERENCES sites (site_id) ON DELETE CASCADE,
    rule_id     text NOT NULL,
    camera_id   text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('line', 'zone')),
    name        text NOT NULL,
    config      jsonb NOT NULL DEFAULT '{}'::jsonb,  -- geometría normalizada y umbrales
    active      boolean NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, rule_id)
);

-- Conteo de puerta por minuto (cruces de línea). Escritura idempotente: el minuto cerrado
-- se escribe una vez con INSERT ... ON CONFLICT DO UPDATE SET count_in = EXCLUDED.count_in.
CREATE TABLE line_counts_minute (
    site_id     text NOT NULL REFERENCES sites (site_id) ON DELETE CASCADE,
    rule_id     text NOT NULL,
    camera_id   text NOT NULL,
    minute      timestamptz NOT NULL CHECK (date_trunc('minute', minute) = minute),
    count_in    integer NOT NULL DEFAULT 0 CHECK (count_in >= 0),
    count_out   integer NOT NULL DEFAULT 0 CHECK (count_out >= 0),
    PRIMARY KEY (site_id, rule_id, minute)
);
CREATE INDEX line_counts_minute_site_minute ON line_counts_minute (site_id, minute);

-- Ocupación de zona (cola de cajas) por minuto.
CREATE TABLE zone_occupancy_minute (
    site_id                 text NOT NULL REFERENCES sites (site_id) ON DELETE CASCADE,
    rule_id                 text NOT NULL,
    camera_id               text NOT NULL,
    minute                  timestamptz NOT NULL CHECK (date_trunc('minute', minute) = minute),
    samples                 integer NOT NULL CHECK (samples > 0),
    avg_people              real NOT NULL CHECK (avg_people >= 0),
    max_people              integer NOT NULL CHECK (max_people >= 0),
    seconds_over_threshold  integer NOT NULL DEFAULT 0 CHECK (seconds_over_threshold BETWEEN 0 AND 60),
    PRIMARY KEY (site_id, rule_id, minute)
);
CREATE INDEX zone_occupancy_minute_site_minute ON zone_occupancy_minute (site_id, minute);

-- Alertas de cola (inicio/fin). alert_id lo genera la analítica (uuid4) para reintentos idempotentes.
CREATE TABLE queue_alerts (
    alert_id      uuid PRIMARY KEY,
    site_id       text NOT NULL REFERENCES sites (site_id) ON DELETE CASCADE,
    rule_id       text NOT NULL,
    camera_id     text NOT NULL,
    started_at    timestamptz NOT NULL,
    ended_at      timestamptz,
    peak_people   integer NOT NULL CHECK (peak_people >= 0),
    threshold     integer NOT NULL CHECK (threshold >= 1),
    notified_at   timestamptz,
    notify_error  text,
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE INDEX queue_alerts_site_started ON queue_alerts (site_id, started_at);

-- Último latido de cada sede (lo escribe el backend VMS de la tienda cada VMS_HEARTBEAT_SECONDS).
CREATE TABLE site_heartbeats (
    site_id    text PRIMARY KEY REFERENCES sites (site_id) ON DELETE CASCADE,
    last_seen  timestamptz NOT NULL,
    hostname   text NOT NULL DEFAULT '',
    version    text NOT NULL DEFAULT '',
    status     text NOT NULL CHECK (status IN ('ok', 'degraded', 'down')),
    payload    jsonb NOT NULL DEFAULT '{}'::jsonb   -- resumen de salud (ver CONTRATO §7.3)
);

-- Informe semanal por sede (semana ISO, lunes).
CREATE TABLE weekly_reports (
    site_id        text NOT NULL REFERENCES sites (site_id) ON DELETE CASCADE,
    week_start     date NOT NULL CHECK (extract(isodow FROM week_start) = 1),
    generated_at   timestamptz NOT NULL DEFAULT now(),
    status         text NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'error')),
    provider       text NOT NULL DEFAULT '',
    model          text NOT NULL DEFAULT '',
    metrics        jsonb NOT NULL DEFAULT '{}'::jsonb,  -- cifras calculadas en SQL (fuente de verdad)
    body_markdown  text NOT NULL DEFAULT '',            -- redacción del proveedor LLM
    error          text,
    PRIMARY KEY (site_id, week_start)
);
