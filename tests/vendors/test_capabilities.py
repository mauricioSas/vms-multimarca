"""Capacidades de la interfaz que usa B6 (TIME_READ, SECURITY_READ) y «Corregir códec» con copia y deshacer."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.vendors.conftest import TEST_PASSWORD, asgi, make_device
from tools.mocks.dahua import DahuaChannel, DahuaMock
from tools.mocks.hikvision import HikChannel, HikvisionMock
from tools.mocks.onvif import OnvifMock
from vms.core.errors import DeviceAuthFailed, DeviceUnsupported, NotFoundError, ValidationFailed
from vms.core.interfaces import Capability, DeviceClockClient, DeviceSecurityClient
from vms.vendors import client_for
from vms.vendors.codec import CodecFixClient
from vms.vendors.codecfix import CodecFixer
from vms.vendors.registry import REGISTRY


def test_capabilities_are_backed_by_the_client_protocols() -> None:
    """Un driver que declara TIME_READ/SECURITY_READ/API_CODEC_FIX tiene un cliente que lo implementa."""
    for spec in REGISTRY.values():
        dev = make_device(spec.id)
        try:
            client = client_for(dev, "x")
        except DeviceUnsupported:
            assert not spec.capabilities & {Capability.TIME_READ, Capability.SECURITY_READ, Capability.API_CODEC_FIX}
            continue
        if Capability.TIME_READ in spec.capabilities:
            assert isinstance(client, DeviceClockClient), spec.id
        if Capability.SECURITY_READ in spec.capabilities:
            assert isinstance(client, DeviceSecurityClient), spec.id
        if Capability.API_CODEC_FIX in spec.capabilities:
            assert isinstance(client, CodecFixClient), spec.id


def test_clients_declare_what_they_implement() -> None:
    """Regresión (revisión v2): el otro sentido. B6 pregunta la capacidad al registro: si el cliente que da
    `client_for()` sabe leer la hora o los ajustes de seguridad, el driver tiene que declararlo (los perfiles
    ONVIF —Axis, Uniview, VIGI…— reciben `OnvifClient` y no lo declaraban)."""
    missing: list[str] = []
    for spec in REGISTRY.values():
        try:
            client = client_for(make_device(spec.id), "x")
        except DeviceUnsupported:
            continue
        if isinstance(client, DeviceClockClient) and Capability.TIME_READ not in spec.capabilities:
            missing.append(f"{spec.id}: time_read")
        if isinstance(client, DeviceSecurityClient) and Capability.SECURITY_READ not in spec.capabilities:
            missing.append(f"{spec.id}: security_read")
        if isinstance(client, CodecFixClient) and Capability.API_CODEC_FIX not in spec.capabilities:
            missing.append(f"{spec.id}: api_codec_fix")
    assert missing == []


@pytest.mark.parametrize("offset", [0, 7200, -7200])
async def test_device_time_hikvision(offset: int) -> None:
    mock = HikvisionMock(password=TEST_PASSWORD, kind="camera", clock_offset_s=offset)
    client = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        t = await client.device_time()   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert t.source == "isapi" and t.time_mode == "ntp" and t.ntp_server == "pool.ntp.org"
    assert abs(t.skew_s - offset) < 3 and t.device_time.tzinfo is not None


async def test_device_time_dahua_manual() -> None:
    mock = DahuaMock(password=TEST_PASSWORD, kind="camera", clock_offset_s=-60, ntp_enable=False)
    client = client_for(make_device("dahua"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        t = await client.device_time()   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert t.source == "cgi" and t.time_mode == "manual" and abs(t.skew_s + 60) < 3


@pytest.mark.parametrize("offset", [0, 7200, -7200])
async def test_onvif_clock_skew_is_corrected_and_read(offset: int) -> None:
    """Reloj del equipo ±2 h: la contraseña buena sigue valiendo (UsernameToken ajustado) y se mide el desfase."""
    mock = OnvifMock(password=TEST_PASSWORD, clock_offset_s=offset, media2=True)
    dev = make_device("onvif")
    client = client_for(dev, TEST_PASSWORD, transport=asgi(mock.app))
    try:
        info = await client.probe()
        t = await client.device_time()   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert info.model == mock.model and info.mac == "c4:2f:90:f1:e6:a1"
    assert t.source == "onvif" and abs(t.skew_s - offset) < 3 and t.time_mode == "ntp"
    assert mock.rejected == 0


async def test_security_settings_with_temporary_admin(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    hik = HikvisionMock(password=TEST_PASSWORD, kind="camera", username="admin")
    client = client_for(make_device("hikvision", username="visor"), "otra", transport=asgi(hik.app))
    try:
        s = await client.security_settings("admin", TEST_PASSWORD)   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert (s.telnet_enabled, s.ssh_enabled, s.upnp_enabled, s.p2p_cloud_enabled) == (False, False, True, True)
    assert s.http_enabled is True and s.https_enabled is False and s.sdk_port_open is True
    assert TEST_PASSWORD not in caplog.text and TEST_PASSWORD not in json.dumps(s.raw)

    dah = DahuaMock(password=TEST_PASSWORD, kind="camera")
    client = client_for(make_device("dahua"), TEST_PASSWORD, transport=asgi(dah.app))
    try:
        s = await client.security_settings("admin", TEST_PASSWORD)   # type: ignore[attr-defined]
        with pytest.raises(DeviceAuthFailed):
            await client.security_settings("admin", "Mala#1")        # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert (s.telnet_enabled, s.upnp_enabled, s.p2p_cloud_enabled, s.https_enabled) == (False, True, True, False)

    onv = OnvifMock(password=TEST_PASSWORD, anonymous=True)
    client = client_for(make_device("onvif"), TEST_PASSWORD, transport=asgi(onv.app))
    try:
        s = await client.security_settings("admin", TEST_PASSWORD)   # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert s.anonymous_onvif is True


# --------------------------------------------------------------------------- «Corregir códec»
@pytest.mark.parametrize("vendor", ["hikvision", "dahua"])
async def test_codec_fix_backup_apply_undo_and_audit(vendor: str, tmp_path: Path,
                                                     caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="vms.audit")
    if vendor == "hikvision":
        mock: Any = HikvisionMock(password=TEST_PASSWORD, channels=[HikChannel("A"), HikChannel("B", sub_codec="H.265")])
        sub = lambda: mock.channels[1].sub_codec  # noqa: E731
    else:
        mock = DahuaMock(password=TEST_PASSWORD, channels=[DahuaChannel("A"), DahuaChannel("B", sub_codec="H.265")])
        sub = lambda: mock.channels[1].sub_codec  # noqa: E731
    fixer = CodecFixer(tmp_path)
    client = client_for(make_device(vendor, kind="nvr"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        rec = await fixer.apply("dev-00000001", client, 2, user="ana", ip="10.0.0.5")
        assert sub() == "H.264" and rec.previous_codec == "H.265" and rec.new_codec == "H.264"
        files = sorted(p.name for p in (tmp_path / "device-backups" / "dev-00000001").iterdir())
        assert files == [f"{rec.backup_id}.json", f"{rec.backup_id}.{'xml' if vendor == 'hikvision' else 'txt'}"]
        backup = (tmp_path / "device-backups" / "dev-00000001" / files[1]).read_text()
        assert "H.265" in backup and TEST_PASSWORD not in backup
        with pytest.raises(ValidationFailed):
            await fixer.apply("dev-00000001", client, 2, user="ana", ip="10.0.0.5")   # ya es H.264
        hist = fixer.history("dev-00000001")
        assert [h.backup_id for h in hist] == [rec.backup_id] and hist[0].undo_available
        undone = await fixer.undo("dev-00000001", client, rec.backup_id, user="luis", ip="10.0.0.6")
        assert sub() == "H.265" and undone.undone_at is not None
        with pytest.raises(ValidationFailed):
            await fixer.undo("dev-00000001", client, rec.backup_id, user="luis", ip="10.0.0.6")
    finally:
        await client.aclose()
    if vendor == "hikvision":
        assert len(mock.puts) == 2 and "<videoCodecType>H.264</videoCodecType>" in mock.puts[0][1]
        assert mock.puts[1][1].count("H.265") == 1     # el deshacer repone el XML exacto
    else:
        assert mock.sets == ["Encode[1].ExtraFormat[0].Video.Compression=H.264",
                             "Encode[1].ExtraFormat[0].Video.Compression=H.265"]
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == "vms.audit"]
    assert [ln["event"] for ln in lines] == ["codec_fix", "codec_fix_undo"]
    assert lines[0]["user"] == "ana" and lines[0]["previous"] == "H.265" and lines[0]["new"] == "H.264"
    assert lines[1]["user"] == "luis" and lines[1]["channel"] == 2


async def test_codec_fix_undo_expires_after_30_days(tmp_path: Path) -> None:
    mock = HikvisionMock(password=TEST_PASSWORD, kind="camera", channels=[HikChannel("A", sub_codec="H.265")])
    now = [datetime(2026, 10, 1, tzinfo=timezone.utc)]
    fixer = CodecFixer(tmp_path, clock=lambda: now[0])
    client = client_for(make_device("hikvision"), TEST_PASSWORD, transport=asgi(mock.app))
    try:
        rec = await fixer.apply("dev-00000002", client, 1, user="ana", ip="-")
        now[0] += timedelta(days=31)
        with pytest.raises((ValidationFailed, NotFoundError)):
            await fixer.undo("dev-00000002", client, rec.backup_id, user="ana", ip="-")
        assert fixer.history("dev-00000002") == []          # la copia caducada se borra
        with pytest.raises(NotFoundError):
            await fixer.undo("dev-00000002", client, "../../etc", user="ana", ip="-")
    finally:
        await client.aclose()


async def test_codec_fix_not_offered_for_profiles(tmp_path: Path) -> None:
    with pytest.raises(DeviceUnsupported):
        await CodecFixer(tmp_path).apply("dev-00000003", object(), 1, user="a", ip="-")
