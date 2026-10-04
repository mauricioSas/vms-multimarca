from __future__ import annotations

import pytest

from vms.core import rtsp
from vms.core.naming import is_valid_id, mtx_path, new_id, parse_mtx_path


@pytest.mark.parametrize("vendor,channel,expected", [
    ("hikvision", 1, ("/Streaming/Channels/101", "/Streaming/Channels/102")),
    ("hikvision", 12, ("/Streaming/Channels/1201", "/Streaming/Channels/1202")),
    ("dahua", 3, ("/cam/realmonitor?channel=3&subtype=0", "/cam/realmonitor?channel=3&subtype=1")),
    ("onvif", 1, None),
    ("generic", 1, None),
])
def test_preset_paths(vendor: str, channel: int, expected: tuple[str, str] | None) -> None:
    assert rtsp.preset_paths(vendor, channel) == expected


@pytest.mark.parametrize("bad", [0, -1, 513])
def test_preset_rejects_bad_channel(bad: int) -> None:
    with pytest.raises(ValueError):
        rtsp.preset_paths("hikvision", bad)


def test_build_url_encodes_credentials_and_ipv6() -> None:
    url = rtsp.build_rtsp_url("192.168.1.10", 554, "Streaming/Channels/101", "admin", "p@ss:w/rd#1%")
    assert url == "rtsp://admin:p%40ss%3Aw%2Frd%231%25@192.168.1.10:554/Streaming/Channels/101"
    assert rtsp.build_rtsp_url("fe80::1", 554, "/a") == "rtsp://[fe80::1]:554/a"


@pytest.mark.parametrize("text,leak", [
    ("abriendo rtsp://admin:S3cret%40x@10.0.0.2:554/Streaming/Channels/101", "S3cret"),
    ("source: rtsps://u:p4ss@host/x", "p4ss"),
    ("GET http://admin:clave@cam/ISAPI/System/deviceInfo", "clave"),
    ("postgresql://vms:superclave@db:5432/vms", "superclave"),
    ("https://cam/x?user=admin&password=hunter2&ch=1", "hunter2"),
    ("Authorization: Digest username=\"admin\", response=\"abc123\"", "abc123"),
    ("POST https://api.telegram.org/bot123456:AAHsecretTOKEN/sendMessage", "AAHsecretTOKEN"),
])
def test_redact_hides_secrets(text: str, leak: str) -> None:
    out = rtsp.redact(text)
    assert leak not in out
    assert "***" in out


def test_redact_keeps_harmless_text() -> None:
    assert rtsp.redact("rtsp://10.0.0.2:554/Streaming/Channels/101") == "rtsp://10.0.0.2:554/Streaming/Channels/101"
    assert rtsp.redact("") == ""


@pytest.mark.parametrize("host,ok", [("192.168.1.10", True), ("nvr-tienda.local", True), ("fe80::1", True),
                                      ("[fe80::1]", True), ("192.168.1.300", False), ("", False), ("a b", False)])
def test_is_valid_host(host: str, ok: bool) -> None:
    assert rtsp.is_valid_host(host) is ok


def test_ids_and_mtx_paths() -> None:
    cid = new_id("cam")
    assert is_valid_id(cid) and cid.startswith("cam-")
    assert mtx_path(cid, "sub") == f"{cid}/sub"
    assert parse_mtx_path(f"{cid}/main") == (cid, "main")
    assert parse_mtx_path("sim/hik1/ch1/main") is None
    with pytest.raises(ValueError):
        mtx_path("../etc", "main")
    with pytest.raises(ValueError):
        mtx_path(cid, "third")  # type: ignore[arg-type]
