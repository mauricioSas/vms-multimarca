"""Esqueletos de la fase 0 de la v2: lo que los bloques dan por hecho existe y no rompe la v1."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from vms.api.events import EVENT_ENGINE, EVENT_UPDATE, V2_EVENTS, EventBus, publish_engine, publish_update
from vms.api.routes import ROUTERS, V2_ROUTERS
from vms.core.config_migrations import MIGRATIONS, migrate, newer_than_supported
from vms.core.config_store import ConfigStore
from vms.core.interfaces import Capability, DriverPublic, DriverSpec
from vms.core.models import (CONFIG_VERSION, AppConfig, Device, DeviceCreate, SystemSettings, known_fields_only,
                             known_vendor_ids, register_vendor_ids)

WEB = Path(__file__).resolve().parents[2] / "vms" / "web"


def test_v2_routers_are_registered_before_pages() -> None:
    for name, router in V2_ROUTERS.items():
        assert router in ROUTERS, f"router {name} sin registrar"
    assert ROUTERS[-1].tags == ["pages"] or ROUTERS.index(V2_ROUTERS["vendors"]) < len(ROUTERS) - 1


def test_v2_sse_events_are_declared() -> None:
    assert set(V2_EVENTS) == {"engine", "update", "health", "bookmark", "evidence", "notice"}
    bus = EventBus()
    q = bus.subscribe()
    publish_engine(bus, {"state": "restarted", "pid": 1, "at": "2026-10-05T00:00:00Z"})
    publish_update(bus, {"version": "2.1.0", "viewer_restart": True})
    assert [q.get_nowait()[0] for _ in range(2)] == [EVENT_ENGINE, EVENT_UPDATE]


def test_config_v1_is_migrated_and_unknown_fields_survive(tmp_path: Path) -> None:
    v1 = {"version": 1, "devices": [{"id": "dev-00000001", "name": "NVR", "vendor": "hikvision", "host": "10.0.0.2",
                                     "password": "en-claro", "campo_de_la_2_1": {"x": 1}}],
          "cameras": [], "settings": {"retention": {"days": 15, "ajuste_nuevo": True}}, "seccion_nueva": [1, 2]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(v1), encoding="utf-8")
    cfg, warning = ConfigStore(path).load()
    assert cfg.version == CONFIG_VERSION == 2
    dumped = json.loads(cfg.model_dump_json())
    assert dumped["devices"][0]["campo_de_la_2_1"] == {"x": 1}          # se conserva (rollback seguro)
    assert "password" not in dumped["devices"][0]                        # nunca una contraseña en claro
    assert dumped["settings"]["retention"]["ajuste_nuevo"] is True
    assert dumped["seccion_nueva"] == [1, 2]
    assert dumped["devices"][0]["allow_basic"] is False and dumped["devices"][0]["follow_ip"] is False
    assert warning and "contraseña" in warning


def test_migration_chain_and_newer_versions() -> None:
    assert set(MIGRATIONS) == set(range(1, CONFIG_VERSION))
    doc, applied = migrate({"version": 1})
    assert doc["version"] == CONFIG_VERSION and applied == [1]
    newer = {"version": CONFIG_VERSION + 1}
    assert newer_than_supported(newer) and migrate(newer) == (newer, [])


def test_vendor_is_text_validated_against_the_registry() -> None:
    # una marca desconocida guardada (p. ej. tras un rollback) se conserva…
    dev = Device(name="X", vendor="marca-ficticia", host="10.0.0.3")
    assert dev.vendor == "marca-ficticia"
    # …pero no se puede dar de alta hasta que el registro la conozca
    with pytest.raises(ValueError):
        DeviceCreate(name="X", vendor="marca-ficticia", host="10.0.0.3")
    register_vendor_ids(["marca-ficticia"])
    try:
        assert DeviceCreate(name="X", vendor="marca-ficticia", host="10.0.0.3").vendor == "marca-ficticia"
    finally:
        from vms.core import models
        models._KNOWN_VENDORS.discard("marca-ficticia")
    assert {"hikvision", "dahua", "onvif", "generic"} <= known_vendor_ids()


def test_known_fields_only_drops_unknown_keys_recursively() -> None:
    body = {"retention": {"days": 10, "inventado": 1}, "otro": 2, "site": {"name": "Tienda", "x": 3}}
    assert known_fields_only(SystemSettings, body) == {"retention": {"days": 10}, "site": {"name": "Tienda"}}


def test_driver_spec_public_has_no_callables() -> None:
    spec = DriverSpec(id="demo", name="Demo", brands=("Demo",), kinds=("camera",),
                      capabilities=frozenset({Capability.ONVIF, Capability.TIME_READ}), default_ports={"rtsp": 554},
                      auth=("digest-md5",), maturity="experimental", detect=lambda _h: 0.0)
    pub = spec.public()
    assert isinstance(pub, DriverPublic) and pub.capabilities == ["onvif", "time_read"]
    json.dumps(pub.model_dump())


@pytest.mark.parametrize(("page", "ids"), [
    ("index.html", ["device-form-root", "onboarding-root", "help-root"]),
    ("status.html", ["updates-root", "health-root", "security-audit-root", "notifications-root"]),
    ("playback.html", ["timeline-events-root", "bookmarks-root", "evidence-root"]),
    ("analytics.html", ["counts-export-root"]),
])
def test_empty_containers_exist(page: str, ids: list[str]) -> None:
    html = (WEB / "pages" / page).read_text(encoding="utf-8")
    for i in ids:
        assert f'id="{i}"' in html


def test_devices_js_is_split_from_panel_js() -> None:
    panel = (WEB / "static" / "js" / "panel.js").read_text(encoding="utf-8")
    devices = (WEB / "static" / "js" / "devices.js").read_text(encoding="utf-8")
    assert 'from "./devices.js"' in panel and "export function openDeviceDialog" in devices
    assert "function renderDevices" not in panel


def test_default_config_round_trips() -> None:
    cfg = AppConfig()
    assert AppConfig.model_validate_json(cfg.model_dump_json()) == cfg


def test_ops_models_round_trip_and_have_spanish_texts() -> None:
    from datetime import datetime, timezone

    from vms.ops.models import (HEALTH_CAUSE_ES, Advisory, AdvisoryTable, CameraHealth, EvidenceManifest,
                                HealthCause, HealthCheck, OnboardingState, RetentionForecast)

    assert set(HEALTH_CAUSE_ES) == {c.value for c in HealthCause}
    now = datetime(2026, 10, 5, tzinfo=timezone.utc)
    check = HealthCheck(camera_id="cam-00000001", score=0, status="critical", causes=[HealthCause.COVERED])
    health = CameraHealth(camera_id="cam-00000001", score=0, status="critical", last_check=check)
    assert CameraHealth.model_validate_json(health.model_dump_json()) == health
    table = AdvisoryTable(generated_at=now, advisories=[Advisory(
        id="ADV-2026-001", vendor="hikvision", model_regex="^DS-2CD", compare="build_date",
        fixed_build_date="2021-06-28", cves=["CVE-2021-36260"], kev=True, reviewed="2026-10-05")])
    assert table.model_dump(by_alias=True)["schema"] == 1
    manifest = EvidenceManifest(product_version="2.0.0", export_id="ev-20261005-abcdef", created_at=now,
                                created_by="admin", reason="Hurto en cajas", site={"id": "s0037"},
                                range_utc=(now, now), range_local=("a", "b"), cameras=[], files=[],
                                signing_key={"algorithm": "ed25519"})
    assert manifest.model_dump(by_alias=True)["schema"] == 1
    assert RetentionForecast(target_days=30, forecast_days=12.5, disk_total=1, disk_free=1, reclaimable=0).status == "ok"
    assert OnboardingState(username="admin").wizard_completed is False
