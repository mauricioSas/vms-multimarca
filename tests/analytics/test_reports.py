"""Informe semanal: cifras SQL en hora local, plantilla sin LLM, validación de cifras y guardado."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from pydantic import SecretStr

from analytics.reports import generate_weekly_report, previous_iso_week_start
from analytics.reports.__main__ import main as report_cli
from analytics.reports.llm import AnthropicProvider, LlmError, provider_from_settings
from analytics.reports.render import markdown_to_html, unknown_numbers
from vms.core.settings import VmsSettings

pytestmark = [pytest.mark.needs_postgres]

SITE = "site-bcn-001"
WEEK = date(2026, 9, 28)          # lunes; Madrid en horario de verano (UTC+2)
UTC = timezone.utc


def local(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=UTC) - timedelta(hours=2)


def seed(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO sites (site_id, name, timezone) VALUES (%s, 'Tienda Gràcia', 'Europe/Madrid')", (SITE,))
        conn.execute("""INSERT INTO analytics_rules (site_id, rule_id, camera_id, kind, name, config) VALUES
            (%s, 'rule-line', 'cam-1', 'line', 'Entrada principal', '{}'),
            (%s, 'rule-zone', 'cam-2', 'zone', 'Cajas 1-3', '{"alert_threshold": 5}')""", (SITE, SITE))
        rows = []
        for m in range(60):
            rows.append((local(WEEK, 10, m), 2, 1))                       # lunes 10 h: 120 entradas
            rows.append((local(WEEK + timedelta(days=5), 18, m), 5, 4))   # sábado 18 h: 300 entradas
            rows.append((local(WEEK - timedelta(days=7), 11, m), 5, 5))   # semana anterior: 300
        rows.append((datetime(2026, 9, 27, 22, 30, tzinfo=UTC), 1, 0))   # lunes 28 a las 00:30 locales: cuenta
        rows.append((datetime(2026, 10, 4, 22, 30, tzinfo=UTC), 1, 0))   # lunes 5 a las 00:30 locales: no cuenta
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO line_counts_minute (site_id, rule_id, camera_id, minute, count_in, count_out) "
                            "VALUES (%s, 'rule-line', 'cam-1', %s, %s, %s)", [(SITE, *r) for r in rows])
            cur.executemany("INSERT INTO zone_occupancy_minute (site_id, rule_id, camera_id, minute, samples, "
                            "avg_people, max_people, seconds_over_threshold) VALUES (%s, 'rule-zone', 'cam-2', %s, "
                            "60, 4.0, 7, 30)", [(SITE, local(WEEK + timedelta(days=5), 18, m)) for m in range(60)])
            alerts = [(local(WEEK + timedelta(days=5), 18, 0), 10, 7), (local(WEEK + timedelta(days=5), 18, 30), 20, 8),
                      (local(WEEK - timedelta(days=2), 12, 0), 5, 6)]
            cur.executemany("INSERT INTO queue_alerts (alert_id, site_id, rule_id, camera_id, started_at, ended_at, "
                            "peak_people, threshold) VALUES (gen_random_uuid(), %s, 'rule-zone', 'cam-2', %s, %s, %s, 5)",
                            [(SITE, s, s + timedelta(minutes=d), p) for s, d, p in alerts])


class FakeProvider:
    name = "fake"
    model = "fake-1"

    def __init__(self, text: str | Exception) -> None:
        self.text = text
        self.prompts: list[str] = []

    async def complete(self, system: str, prompt: str, *, max_tokens: int = 1500) -> str:
        self.prompts.append(prompt)
        if isinstance(self.text, Exception):
            raise self.text
        return self.text


async def test_metrics_template_and_storage(pg_dsn: str) -> None:
    seed(pg_dsn)
    rep = await generate_weekly_report(pg_dsn, SITE, WEEK, None)
    d, q = rep.metrics["doors"], rep.metrics["queues"]
    assert d["total_in"] == 421 and d["previous_total_in"] == 300
    assert d["change_in_pct"] == 40.3
    assert d["peak_hour"] == {"hour": 18, "in": 300}                      # hora LOCAL, no UTC
    assert d["busiest_day"]["weekday"] == "sábado" and d["busiest_day"]["date"] == "2026-10-03"
    assert d["by_day"][0]["in"] == 121 and d["by_hour"][0]["in"] == 1
    assert d["lines"][0]["name"] == "Entrada principal"
    (z,) = q["zones"]
    assert z["name"] == "Cajas 1-3" and z["avg_people"] == 4.0 and z["max_people"] == 7
    assert z["minutes_over_threshold"] == 30.0 and z["busiest_hour"]["hour"] == 18 and z["threshold"] == 5
    a = q["alerts"]
    assert (a["count"], a["previous_count"], a["avg_minutes"], a["max_minutes"], a["max_peak_people"]) == (2, 1, 15.0, 20.0, 8)
    # Plantilla sin LLM: estado ok, texto en español con las cifras
    assert rep.status == "ok" and rep.provider == "template" and rep.error is None
    assert "421 personas" in rep.narrative and "40,3 %" in rep.narrative and "sábado 3 de octubre" in rep.narrative
    assert "2 avisos de cola" in rep.narrative
    assert unknown_numbers(rep.narrative, rep.metrics) == []           # la plantilla no inventa
    assert "<table>" in rep.html and "Tienda Gràcia" in rep.html and "<strong>421 personas</strong>" in rep.html
    with psycopg.connect(pg_dsn) as conn:
        row = conn.execute("SELECT status, provider, metrics->'doors'->>'total_in', body_markdown FROM weekly_reports "
                           "WHERE site_id = %s AND week_start = %s", (SITE, WEEK)).fetchone()
    assert row is not None and row[:3] == ("ok", "template", "421") and row[3] == rep.markdown


async def test_llm_text_accepted_when_numbers_are_real(pg_dsn: str) -> None:
    seed(pg_dsn)
    prov = FakeProvider("Esta semana entraron **421 personas**, un 40,3 % más que la anterior (300). "
                        "El pico fue el sábado entre las 18 y las 19 h, con 2 avisos de cola de 15 minutos de media.")
    rep = await generate_weekly_report(pg_dsn, SITE, WEEK, prov)  # type: ignore[arg-type]
    assert rep.status == "ok" and rep.provider == "fake" and rep.narrative.startswith("Esta semana entraron")
    assert '"total_in": 421' in prov.prompts[0]          # el proveedor solo recibe agregados
    assert "rtsp" not in prov.prompts[0].lower()


async def test_llm_invented_numbers_fall_back_to_template(pg_dsn: str) -> None:
    seed(pg_dsn)
    prov = FakeProvider("Entraron 1.999 personas y la cola media fue de 12,5 personas.")
    rep = await generate_weekly_report(pg_dsn, SITE, WEEK, prov)  # type: ignore[arg-type]
    assert rep.status == "error" and "1.999" in (rep.error or "")
    assert "421 personas" in rep.narrative                # se usó la plantilla


async def test_llm_failure_still_produces_report(pg_dsn: str) -> None:
    seed(pg_dsn)
    rep = await generate_weekly_report(pg_dsn, SITE, WEEK, FakeProvider(LlmError("Sin conexión con el proveedor LLM")),  # type: ignore[arg-type]
                                       store=True)
    assert rep.status == "error" and rep.error == "Sin conexión con el proveedor LLM"
    with psycopg.connect(pg_dsn) as conn:
        assert conn.execute("SELECT status, error FROM weekly_reports").fetchone() == (
            "error", "Sin conexión con el proveedor LLM")


async def test_empty_week_and_unknown_site(pg_dsn: str) -> None:
    seed(pg_dsn)
    rep = await generate_weekly_report(pg_dsn, SITE, WEEK + timedelta(days=28), None, store=False)
    assert rep.metrics["doors"]["total_in"] == 0 and "No hay entradas" in rep.narrative
    with pytest.raises(LookupError):
        await generate_weekly_report(pg_dsn, "site-no-existe", WEEK, None)
    with pytest.raises(ValueError):
        await generate_weekly_report(pg_dsn, SITE, WEEK + timedelta(days=1), None)


def test_cli_writes_markdown_and_html(pg_dsn: str, tmp_path: Path) -> None:
    seed(pg_dsn)
    rc = report_cli(["--site", SITE, "--week", WEEK.isoformat(), "--dsn", pg_dsn, "--no-llm", "--out", str(tmp_path)])
    assert rc == 0
    md = (tmp_path / f"informe_{SITE}_{WEEK}.md").read_text(encoding="utf-8")
    ht = (tmp_path / f"informe_{SITE}_{WEEK}.html").read_text(encoding="utf-8")
    assert md.startswith("# Informe semanal · Tienda Gràcia") and "2026-W40" in md
    assert ht.startswith("<!doctype html>") and "Entradas por día" in ht


def test_previous_iso_week_start() -> None:
    assert previous_iso_week_start(date(2026, 10, 5)) == date(2026, 9, 28)    # lunes
    assert previous_iso_week_start(date(2026, 10, 4)) == date(2026, 9, 21)    # domingo


def test_unknown_numbers_parsing() -> None:
    m = {"a": 1234, "b": 12.5, "c": 40.3, "d": "2026-10-03"}
    assert unknown_numbers("1.234 personas, 12,5 de media, +40,3 % el 3 de octubre de 2026", m) == []
    assert unknown_numbers("unas 1.500 personas", m) == ["1.500"]
    assert unknown_numbers("1.999 personas", m) == ["1.999"]   # no se lee como 1,999 ≈ 2
    assert unknown_numbers("entre las 18 y las 19 h, 7 días", m) == []        # horas y números pequeños


def test_markdown_to_html_escapes() -> None:
    out = markdown_to_html("Hola <script>alert(1)</script> **x**")
    assert "<script>" not in out and "&lt;script&gt;" in out and "<strong>x</strong>" in out


def test_provider_from_settings(settings: VmsSettings) -> None:
    assert provider_from_settings(settings.model_copy(update={"llm_provider": "none"})) is None
    assert provider_from_settings(settings) is None          # anthropic sin clave → plantilla
    prov = provider_from_settings(settings.model_copy(update={"llm_api_key": SecretStr("sk-prueba"), "llm_model": ""}))
    assert isinstance(prov, AnthropicProvider) and prov.model == "claude-sonnet-5-5"


async def test_anthropic_provider_maps_response_and_errors() -> None:
    class Block:
        def __init__(self, t: str, text: str = "") -> None:
            self.type, self.text = t, text

    class Resp:
        def __init__(self, content: list[Block], stop: str) -> None:
            self.content, self.stop_reason = content, stop

    class Messages:
        def __init__(self, resp: Resp | Exception) -> None:
            self.resp = resp
            self.kwargs: dict[str, object] = {}

        async def create(self, **kw: object) -> Resp:
            self.kwargs = kw
            if isinstance(self.resp, Exception):
                raise self.resp
            return self.resp

    class Client:
        def __init__(self, resp: Resp | Exception) -> None:
            self.messages = Messages(resp)

    ok = Client(Resp([Block("thinking"), Block("text", "Informe redactado.")], "end_turn"))
    prov = AnthropicProvider("k", "modelo-x", client=ok)
    assert await prov.complete("sys", "datos", max_tokens=100) == "Informe redactado."
    assert ok.messages.kwargs["model"] == "modelo-x" and ok.messages.kwargs["system"] == "sys"
    with pytest.raises(LlmError, match="declinó"):
        await AnthropicProvider("k", client=Client(Resp([], "refusal"))).complete("s", "p")
    with pytest.raises(LlmError, match="cortada"):
        await AnthropicProvider("k", client=Client(Resp([Block("text", "a medias")], "max_tokens"))).complete("s", "p")
    import anthropic
    import httpx2

    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.example/v1/messages"))
    with pytest.raises(LlmError, match="Sin conexión"):
        await AnthropicProvider("k", client=Client(err)).complete("s", "p")
