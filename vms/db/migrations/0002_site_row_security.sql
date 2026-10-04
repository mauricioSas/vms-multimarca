-- 0002_site_row_security: cada tienda solo ve y escribe SUS filas.
--
-- Cada PC de tienda se conecta con su propio rol de PostgreSQL (p. ej. «vms_site_bcn_001»),
-- anotado en site_db_roles junto a su site_id. Con Row Level Security, ese rol solo puede leer,
-- insertar o modificar filas con su site_id: un PC de tienda comprometido no puede tocar los
-- conteos, alertas ni nombres de las demás sedes.
--
-- Los roles que NO están en site_db_roles (panel central, informe semanal, administrador) ven
-- todas las filas, como antes. El dueño de las tablas y los superusuarios no están sujetos a RLS.
-- Los roles de tienda se crean con «python -m central site-role create <sede>».

CREATE TABLE site_db_roles (
    role_name   text PRIMARY KEY,
    site_id     text NOT NULL CHECK (site_id ~ '^[a-z0-9][a-z0-9-]{2,39}$'),
    created_at  timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON site_db_roles FROM PUBLIC;

-- Sede del rol con el que se ha iniciado la sesión (session_user: un SET ROLE no la cambia).
-- SECURITY DEFINER para que los roles de tienda no necesiten leer site_db_roles.
DO $$
BEGIN
    EXECUTE format($f$
        CREATE FUNCTION vms_session_site() RETURNS text
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, %I
        AS 'SELECT site_id FROM site_db_roles WHERE role_name = session_user::text'
    $f$, current_schema());
END
$$;
REVOKE ALL ON FUNCTION vms_session_site() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION vms_session_site() TO PUBLIC;

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['sites', 'site_cameras', 'analytics_rules', 'line_counts_minute',
                             'zone_occupancy_minute', 'queue_alerts', 'site_heartbeats', 'weekly_reports']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        -- (SELECT …) se evalúa una vez por consulta, no por fila.
        EXECUTE format($p$
            CREATE POLICY site_rows ON %I
            USING ((SELECT vms_session_site()) IS NULL OR site_id = (SELECT vms_session_site()))
            WITH CHECK ((SELECT vms_session_site()) IS NULL OR site_id = (SELECT vms_session_site()))
        $p$, t);
    END LOOP;
END
$$;
