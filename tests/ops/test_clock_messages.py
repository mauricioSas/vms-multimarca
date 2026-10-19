"""Textos de la hora honestos (CONTRATO §18.3): lo que se leyó de verdad en la cámara del usuario.

Antes, con la cámara a 01-01-1970 en UTC+8 y hora manual, el programa decía en un solo mensaje: «La hora va
497627,8 h atrasada: las grabaciones tendrán la hora equivocada… Activa NTP en el equipo apuntando a este PC… puesta
a mano (sin NTP)… Pon la zona horaria correcta» y el diagnóstico añadía otra vez «Activa NTP…». Tres órdenes que se
contradicen, una de ellas imposible (ese PC no sirve la hora) y falsa sobre las grabaciones (llevan la hora del PC).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.ops.doubles import DiagBehavior, DiagClient
from tests.test_spanish_style import voseo_in
from vms.core.interfaces import DeviceTime
from vms.core.models import Device
from vms.ops import report as report_mod
from vms.ops.diagnose import DiagnoseInput, Diagnoser
from vms.ops.health import clock as clk
from vms.ops.models import ClockCheck
from vms.ops.report import CameraInput

PC_MADRID_SUMMER = 2 * 3600


class WinDatetime(datetime):
    """Como en Windows: `astimezone()` a la hora local falla con instantes antes de 1970."""

    def astimezone(self, tz: Any = None) -> Any:  # type: ignore[override]
        if tz is None and self.timestamp() < 86400:
            raise OSError(22, "Invalid argument")
        return super().astimezone(tz)


def _real_reading() -> DeviceTime:
    t = WinDatetime(1970, 1, 1, 0, 7, 29, tzinfo=timezone(timedelta(hours=8)))
    return DeviceTime(device_time=t, measured_at=datetime.now(timezone.utc), time_mode="manual",
                      utc_offset_s=8 * 3600, source="isapi")


def test_1970_camera_says_it_has_no_time_and_one_thing_to_do() -> None:
    chk = clk.device_check("dev-00000001", None, _real_reading(), 2.0, 30.0, noun="la cámara", pc_serves_time=False)
    msg = chk.message_es
    assert chk.status == "critical"
    assert "La cámara no tiene la hora puesta: marca el 01-01-1970" in msg
    assert "Las grabaciones usan la hora de este PC y son correctas" in msg
    assert "zona horaria (UTC+8) distinta de la de este PC" in msg and "«Poner en hora con este PC»" in msg
    assert msg.count("Qué hacer:") == 1
    for wrong in ("Activa NTP", "apuntando a este PC", "a mano", "497", " h atrasada", "tendrán la hora equivocada"):
        assert wrong not in msg, wrong
    assert voseo_in(msg) == []


def test_ntp_advice_only_when_this_pc_serves_time() -> None:
    now = datetime.now(timezone.utc)
    dt = DeviceTime(device_time=now + timedelta(seconds=12), measured_at=now, time_mode="manual")
    off = clk.device_check("dev-00000001", None, dt, 2.0, 30.0, pc_offset_s=PC_MADRID_SUMMER, pc_serves_time=False)
    on = clk.device_check("dev-00000001", None, dt, 2.0, 30.0, pc_offset_s=PC_MADRID_SUMMER, pc_serves_time=True)
    assert "NTP en el equipo" not in off.message_es and "Poner en hora con este PC" in off.message_es
    assert "NTP en el equipo con la IP de este PC" in on.message_es
    assert off.status == on.status == "warning" and "12,0 s adelantada" in off.message_es


def test_brand_without_time_write_gets_the_web_instead_of_the_button() -> None:
    now = datetime.now(timezone.utc)
    dt = DeviceTime(device_time=now - timedelta(minutes=10), measured_at=now, time_mode="manual")
    chk = clk.device_check("dev-00000001", None, dt, 2.0, 30.0, pc_offset_s=PC_MADRID_SUMMER, can_set=False, noun="la cámara",
                           pc_serves_time=False)
    assert "Poner en hora" not in chk.message_es and "desde la web de la cámara" in chk.message_es


def test_zone_advice_does_not_name_a_button_the_brand_lacks() -> None:
    """Revisión: con la zona distinta y una marca sin `time_write` (Dahua), la acción decía «“Poner en hora con este
    PC” no la cambia», un botón que esa marca no tiene."""
    now = datetime.now(timezone.utc)
    for skew_min, mode in ((-10, "manual"), (0, "ntp")):   # con la hora mal y con la hora bien (solo la zona)
        dt = DeviceTime(device_time=now + timedelta(minutes=skew_min), measured_at=now, time_mode=mode,
                        utc_offset_s=8 * 3600)
        msg = clk.device_check("dev-00000001", None, dt, 2.0, 30.0, pc_offset_s=PC_MADRID_SUMMER, can_set=False,
                               noun="el grabador", pc_serves_time=False).message_es
        assert "zona horaria en la web del grabador" in msg and "Poner en hora" not in msg, msg
        assert msg.count("Qué hacer:") == 1 and voseo_in(msg) == []
    # con NTP el botón no escribe nada (ni la hora ni la zona): la zona se cambia en la web del equipo
    with_button = clk.assess(0.1, "ntp", 2, 30, zone="La cámara usa otra zona.", can_set=True, noun="la cámara")
    assert "zona horaria en la web de la cámara" in with_button.action and "Poner en hora" not in with_button.action


def test_ntp_device_is_told_to_check_its_server_not_the_button() -> None:
    now = datetime.now(timezone.utc)
    dt = DeviceTime(device_time=now + timedelta(hours=2), measured_at=now, time_mode="ntp", ntp_server="10.0.0.1")
    chk = clk.device_check("dev-00000001", None, dt, 2.0, 30.0, pc_offset_s=PC_MADRID_SUMMER, noun="el grabador",
                           pc_serves_time=False)
    assert "servidor de hora NTP (10.0.0.1)" in chk.message_es and "Poner en hora con este PC»." not in chk.message_es
    assert "La hora del grabador va 2,0 h adelantada" in chk.message_es


def test_large_skews_in_days_and_years() -> None:
    assert clk.fmt_skew(-1_791_489_509.3) == "−57 años"
    assert clk.fmt_skew(3 * 86400) == "+3 días" and clk.fmt_skew(-5400) == "−1,5 h" and clk.fmt_skew(None) == "—"
    assert "5 días atrasada" in clk.evaluate(-5 * 86400, "ntp", 2, 30)[1]


def test_report_problem_does_not_show_raw_seconds() -> None:
    day = datetime.now(timezone.utc).date()
    start, end = report_mod.day_bounds(day, "Europe/Madrid")
    cam = CameraInput(camera_id="cam-00000001", name="Caja", record=False, online_now=True, spans=None,
                      score_min=None, causes=[], clock=ClockCheck(device_id="dev-00000001", skew_s=-1_791_489_509.3,
                                                                  status="critical", message_es="x"),
                      days_on_disk=None, protected=0)
    rep = report_mod.build("sede", day, "Europe/Madrid", [cam], now=end, forecast=None, disk={}, pc_clock=None,
                           protected_oldest_days=None)
    assert "Caja: hora desajustada (−57 años)" in rep.problems


async def test_diagnose_clock_step_separates_cause_and_action() -> None:
    """«¿Por qué no conecta?» pinta «Qué hacer: » delante de la acción (ops-common.js): la acción no lo trae, la
    causa tampoco, y ya no dice «Activa NTP en el equipo apuntando a este PC»."""
    b = DiagBehavior()
    reading = _real_reading()

    class WithClock(DiagClient):
        async def device_time(self) -> Any:
            return reading

    async def tcp(host: str, port: int, timeout: float) -> bool:
        return True

    async def http(host: str, port: int, https: bool, timeout: float) -> int:
        return 200

    async def rtsp(*a: Any, **k: Any) -> Any:
        class R:
            status, video_codec, reachable, error = 200, "H264", True, ""
        return R()

    diag = Diagnoser(lambda d, p: WithClock(b, p), tcp_check=tcp, http_probe=http, rtsp_probe=rtsp)
    dev = Device(name="Cámara caja", vendor="hikvision", kind="camera", host="10.0.0.5", username="admin")
    res = await diag.run(DiagnoseInput(dev, b.password, "dev-00000001", []))
    step = next(s for s in res.steps if s.code == "clock")
    assert step.ok is False and "no tiene la hora puesta" in step.detail_es
    assert step.action_es.startswith("En Estado del sistema") and "Qué hacer" not in step.action_es
    assert "Qué hacer" not in step.detail_es and "Activa NTP" not in step.action_es


async def test_evidence_record_shows_a_readable_skew(tmp_path: Path) -> None:
    """Revisión: el acta de cadena de custodia de una exportación ponía «-1791489509.3 s» para la cámara a 1970 (y
    con punto decimal). Ahora la cifra legible y, entre paréntesis, la exacta para sumarla o restarla."""
    import zipfile

    from tests.ops.conftest import write_segments
    from vms.core.paths import AppPaths
    from vms.ops.evidence.export import CameraInfo, EvidenceBuilder, ExportContext
    from vms.ops.evidence.keys import load_or_create
    from vms.ops.models import EvidenceExport, EvidenceExportRequest
    t0 = datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)
    rec = tmp_path / "rec"
    for cam in ("cam-00000001", "cam-00000002"):
        write_segments(rec, cam, t0, 2)
    req = EvidenceExportRequest(camera_ids=["cam-00000001", "cam-00000002"], start=t0, end=t0 + timedelta(minutes=2),
                                reason="Hurto en la caja", include_mp4=False)
    exp = EvidenceExport(export_id="ev-20261004-cccccc", created_by="ana", request=req)
    ctx = ExportContext(product_version="2.0.0", site={"id": "s1", "name": "Tienda", "code": "1", "timezone": "UTC"},
                        cameras={"cam-00000001": CameraInfo("cam-00000001", "Caja", clock_skew_s=-1_791_489_509.3),
                                 "cam-00000002": CameraInfo("cam-00000002", "Puerta", clock_skew_s=3.24)},
                        recordings_dir=rec, protected_dir=tmp_path / "prot", segment_seconds=60,
                        key=load_or_create(AppPaths(tmp_path / "datos").ensure()))
    path, _, _ = await EvidenceBuilder(tmp_path / "exports").build(exp, ctx)
    with zipfile.ZipFile(path) as zf:
        acta = next(zf.read(n).decode("utf-8") for n in zf.namelist() if n.endswith("acta.html"))
    assert "−57 años (−1.791.489.509,3 s)" in acta and "1791489509" not in acta
    assert "+3,2 s" in acta


def test_pc_messages_have_what_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(clk, "windows_time_config", lambda: ("manual", ""))
    chk = clk.pc_check(2.0, 30.0)
    assert chk.status == "warning" and "Qué hacer:" in chk.message_es and "actívalo" not in chk.message_es
    for status, msg in (clk.evaluate_pc(45, "ntp", 2, 30, "t"), clk.evaluate_pc(5, "ntp", 2, 30, "t"),
                        clk.evaluate_pc(0.1, "manual", 2, 30, "t")):
        assert status != "ok" and msg.count("Qué hacer:") == 1 and voseo_in(msg) == []
        assert "Qué hacer: " in msg and msg.split("Qué hacer: ")[1][:1].islower(), msg


def test_pc_time_server_not_answering_says_what_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    """Revisión: «No responde el servidor de hora X» (Windows con NTP) no decía qué hacer."""
    def no_answer(server: str) -> tuple[float, float]:
        raise OSError("timed out")
    monkeypatch.setattr(clk, "windows_time_config", lambda: ("ntp", "time.windows.com"))
    monkeypatch.setattr(clk, "sntp_offset", no_answer)
    chk = clk.pc_check(2.0, 30.0)
    assert chk.status == "unknown" and "No responde el servidor de hora time.windows.com" in chk.message_es
    assert chk.message_es.count("Qué hacer:") == 1 and "Internet" in chk.message_es and voseo_in(chk.message_es) == []


def test_recorder_and_generic_device_get_masculine_pronoun() -> None:
    """Revisión: «ponla en hora desde la web del grabador» (concordancia): con el grabador y el equipo es «ponlo»."""
    now = datetime.now(timezone.utc)
    dt = DeviceTime(device_time=now - timedelta(minutes=10), measured_at=now, time_mode="manual")
    for noun, pron in (("el grabador", "ponlo"), ("el equipo", "ponlo"), ("la cámara", "ponla")):
        msg = clk.device_check("dev-00000001", None, dt, 2.0, 30.0, pc_offset_s=PC_MADRID_SUMMER, can_set=False,
                               noun=noun, pc_serves_time=False).message_es
        assert f"{pron} en hora desde la web" in msg, msg
        assert ("ponla" if pron == "ponlo" else "ponlo") not in msg


def test_set_action_says_it_needs_an_administrator() -> None:
    """Revisión: el paso de «¿Por qué no conecta?» mandaba pulsar un botón que un operador no ve (solo
    administradores, y solo sale cuando hay una medida con aviso)."""
    a = clk.assess(-90.0, "manual", 2, 30, can_set=True, noun="la cámara")
    assert "administrador" in a.action and "Comprobar ahora" in a.action
    ok_manual = clk.assess(0.5, "manual", 2, 30, can_set=True, noun="la cámara")
    assert ok_manual.status == "warning" and "Mírala" not in ok_manual.action
    assert ok_manual.action.startswith("Revisa su hora de vez en cuando en Estado del sistema")


def test_pc_serves_time_reads_the_w32time_ntp_server_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """`pc_time_server_enabled()` (W32Time `TimeProviders\\NtpServer\\Enabled`) sin tocar el registro real."""
    import sys
    import types
    opened: list[str] = []
    values: dict[str, Any] = {"Enabled": 1}

    class Key:
        def __enter__(self) -> "Key":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    def open_key(root: object, path: str) -> Key:
        opened.append(path)
        if "missing" in values:
            raise FileNotFoundError(path)
        return Key()

    def query(key: Key, name: str) -> tuple[Any, int]:
        return values[name], 4
    fake = types.SimpleNamespace(HKEY_LOCAL_MACHINE=object(), OpenKey=open_key, QueryValueEx=query)
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    assert clk.pc_time_server_enabled() is True
    assert opened[-1] == r"SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpServer"
    values["Enabled"] = 0                     # lo normal en un Windows que no es servidor de dominio
    assert clk.pc_time_server_enabled() is False
    values["Enabled"] = "raro"
    assert clk.pc_time_server_enabled() is False
    values["missing"] = True
    assert clk.pc_time_server_enabled() is False
    monkeypatch.setattr(sys, "platform", "linux")
    del values["missing"]
    values["Enabled"] = 1
    assert clk.pc_time_server_enabled() is False, "fuera de Windows no se aconseja lo que no se ha comprobado"
