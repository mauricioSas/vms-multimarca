-- reversible: yes
-- 0003_site_updates: versiones y actualizaciones por sede para el panel central (PLAN-V2 §2.8, CONTRATO §15.7).
--
-- Lo que la sede INFORMA (versión instalada, último resultado…) llega en el latido (payload.update) y se
-- guarda aquí como copia para listar y filtrar sin abrir el JSON. Lo que el panel PIDE (canal, retener,
-- ventana, comprobar ahora, volver atrás) lo escribe el panel y viaja en la respuesta del latido.
-- El panel NO firma nada: solo elige entre canales firmados (la versión de cada canal la decide la firma
-- de `targets`, que vive en la llave física de Unmanned).
-- Expand/contract: solo añade una tabla; la versión anterior del panel la ignora.

CREATE TABLE site_versions (
    site_id                text PRIMARY KEY REFERENCES sites (site_id) ON DELETE CASCADE,
    -- lo que informa la sede (copia del último latido con payload.update)
    installed              text NOT NULL DEFAULT '',
    update_state           text NOT NULL DEFAULT 'unknown',
    last_result            text NOT NULL DEFAULT 'none',
    message_es             text NOT NULL DEFAULT '',
    available              text,
    reported_at            timestamptz,
    -- lo que pide el panel
    channel                text NOT NULL DEFAULT 'stable' CHECK (channel ~ '^[a-z][a-z0-9-]{1,31}$'),
    hold                   boolean NOT NULL DEFAULT false,
    window_local           text NOT NULL DEFAULT '01:00-03:00'
                           CHECK (window_local ~ '^([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]$'),
    rollback_to            text CHECK (rollback_to IS NULL OR rollback_to ~ '^(previous|[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?)$'),
    rollback_requested_at  timestamptz,
    check_requested_at     timestamptz,
    updated_at             timestamptz NOT NULL DEFAULT now(),
    updated_by             text NOT NULL DEFAULT ''
);
CREATE INDEX site_versions_channel ON site_versions (channel);

-- Misma regla que 0002: un rol de tienda solo ve y escribe su fila.
ALTER TABLE site_versions ENABLE ROW LEVEL SECURITY;
CREATE POLICY site_rows ON site_versions
    USING ((SELECT vms_session_site()) IS NULL OR site_id = (SELECT vms_session_site()))
    WITH CHECK ((SELECT vms_session_site()) IS NULL OR site_id = (SELECT vms_session_site()));
