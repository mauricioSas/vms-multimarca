"""Las cámaras vuelven a los monitores al darse de alta (CONTRATO §3.3, `wall_memory` y `place_new_cameras`).

Caso real (08/10/2026): el usuario borró su cámara Hikvision y la volvió a dar de alta. Grababa y se veía en
Reproducción, pero desapareció de «Monitores»: el borrado vació su celda y la cámara nueva (con otro id) no se
ponía en ningún muro, así que el muro se quedaba vacío.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from vms.core import config_migrations as cm
from vms.core.config_store import ConfigStore
from vms.core.models import (WALL_MEMORY_DAYS, WALL_MEMORY_MAX, AppConfig, Camera, Device, PlacedCamera,
                             WallPlacement)

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def _device(cfg: AppConfig, host: str = "192.168.1.64", vendor: str = "hikvision", kind: str = "camera") -> Device:
    dev = Device(name="Entrada", vendor=vendor, kind=kind, host=host, username="admin")
    cfg.devices.append(dev)
    return dev


def _add(cfg: AppConfig, dev: Device, channel: int = 1, *, now: datetime = NOW) -> tuple[Camera, list[PlacedCamera]]:
    cam = Camera(name=f"Canal {channel}", device_id=dev.id, channel=channel)
    cfg.cameras.append(cam)
    return cam, cfg.place_new_cameras([cam], now=now)


def _walls(cfg: AppConfig) -> list[tuple[int, list[str | None]]]:
    return [(w.monitor, list(w.cells)) for w in cfg.walls]


def test_deleting_and_re_adding_a_device_with_one_camera_restores_its_cell() -> None:
    cfg = AppConfig()
    dev = _device(cfg)
    cam, _ = _add(cfg, dev)
    cfg.walls[0].cells = [None, None, cam.id]            # la puso él en la celda 3 del monitor 1
    cfg.remove_device(dev.id, now=NOW)
    assert cfg.walls[0].cells[2] is None, "el borrado sigue quitando la cámara del muro"
    assert [(m.host, m.vendor, m.channel, m.monitor, m.cell) for m in cfg.wall_memory] == [
        ("192.168.1.64", "hikvision", 1, 1, 2)]
    assert "admin" not in json.dumps([m.model_dump(mode="json") for m in cfg.wall_memory]), "sin credenciales"

    again = _device(cfg)                                  # se vuelve a dar de alta: otro equipo, otra cámara
    new, placed = _add(cfg, again, now=NOW + timedelta(minutes=5))
    assert new.id != cam.id
    assert placed == [PlacedCamera(new.id, 1, 2, True)]
    assert cfg.walls[0].cells[:3] == [None, None, new.id], "vuelve a su celda, no a la primera libre"
    assert cfg.wall_memory == [], "lo recordado se gasta al volver a darla de alta"


def test_a_camera_on_two_monitors_goes_back_to_both() -> None:
    cfg = AppConfig()
    dev = _device(cfg)
    cam, _ = _add(cfg, dev)
    cfg.walls[0].cells = [None, cam.id]
    cfg.walls[2].grid = 9
    cfg.walls[2].cells = [None] * 5 + [cam.id]
    cfg.remove_camera(cam.id, now=NOW)
    new, placed = _add(cfg, dev)
    assert placed == [PlacedCamera(new.id, 1, 1, True), PlacedCamera(new.id, 3, 5, True)]


def test_a_new_camera_fills_the_first_free_visible_cell_of_monitor_1() -> None:
    cfg = AppConfig()
    dev = _device(cfg, kind="nvr")
    other = Camera(name="Otra", device_id=dev.id, channel=9)
    cfg.cameras.append(other)
    cfg.walls[0].cells = [other.id, None, other.id]
    cam, placed = _add(cfg, dev, 2)
    assert placed == [PlacedCamera(cam.id, 1, 1, False)]
    assert cfg.walls[0].cells[:4] == [other.id, cam.id, other.id, None]


def test_empty_monitor_1_with_grid_1_is_simply_filled() -> None:
    cfg = AppConfig()
    cfg.walls[0].grid = 1
    cam, placed = _add(cfg, _device(cfg))
    assert placed == [PlacedCamera(cam.id, 1, 0, False)] and cfg.walls[0].grid == 1


def test_occupied_cells_and_hidden_cells_are_never_used() -> None:
    cfg = AppConfig()
    dev = _device(cfg, kind="nvr")
    cams = [Camera(name=f"C{i}", device_id=dev.id, channel=i) for i in range(1, 5)]
    cfg.cameras += cams
    cfg.walls[0].cells = [c.id for c in cams]             # monitor 1 lleno (cuadrícula de 4)
    before = _walls(cfg)
    cam, placed = _add(cfg, dev, 5)
    assert placed == [] and _walls(cfg) == before, "sin hueco a la vista no pisa nada ni usa celdas ocultas"
    assert cfg.walls[0].grid == 4, "no cambia la cuadrícula"


def test_remembered_cell_now_occupied_or_hidden_falls_back_to_monitor_1() -> None:
    cfg = AppConfig()
    dev = _device(cfg, kind="nvr")
    cam, _ = _add(cfg, dev, 1)
    other = Camera(name="Otra", device_id=dev.id, channel=2)
    cfg.cameras.append(other)
    cfg.walls[1].cells = [cam.id]                          # estaba en el monitor 2, celda 1
    cfg.walls[0].cells = [None] * 16
    cfg.remove_camera(cam.id, now=NOW)
    cfg.walls[1].cells = [other.id]                        # mientras tanto, su celda se ocupó
    monitor2 = list(cfg.walls[1].cells)
    new, placed = _add(cfg, dev, 1)
    assert placed == [PlacedCamera(new.id, 1, 0, False)]
    assert cfg.walls[1].cells == monitor2, "el monitor 2 no cambia"

    # celda recordada fuera de la cuadrícula actual (se redujo a 1): tampoco se usa
    cfg.walls[0].cells = [other.id]
    cfg.walls[2].grid = 9
    cfg.walls[2].cells = [None] * 4 + [new.id]
    cfg.remove_camera(new.id, now=NOW)
    cfg.walls[2].grid = 1
    again, placed = _add(cfg, dev, 1)
    assert placed == [PlacedCamera(again.id, 1, 1, False)]
    assert cfg.walls[2].cells[4] is None


def test_new_cameras_never_touch_monitors_2_to_4() -> None:
    cfg = AppConfig()
    dev = _device(cfg, kind="nvr")
    cfg.walls[0].grid = 1
    first, _ = _add(cfg, dev, 1)
    before = _walls(cfg)[1:]
    cams = [Camera(name=f"C{i}", device_id=dev.id, channel=i) for i in range(2, 6)]
    cfg.cameras += cams
    assert cfg.place_new_cameras(cams, now=NOW) == []
    assert _walls(cfg)[1:] == before and cfg.walls[0].cells[0] == first.id


def test_restored_cameras_go_first_in_a_batch() -> None:
    """En una importación de varios canales, la que vuelve a su sitio no se lo deja quitar por una nueva."""
    cfg = AppConfig()
    dev = _device(cfg, kind="nvr")
    cam, _ = _add(cfg, dev, 2)
    cfg.walls[0].cells = [cam.id]
    cfg.remove_device(dev.id, now=NOW)
    again = _device(cfg, kind="nvr")
    cams = [Camera(name=f"C{i}", device_id=again.id, channel=i) for i in (1, 2, 3)]
    cfg.cameras += cams
    placed = cfg.place_new_cameras(cams, now=NOW)
    assert placed == [PlacedCamera(cams[1].id, 1, 0, True), PlacedCamera(cams[0].id, 1, 1, False),
                      PlacedCamera(cams[2].id, 1, 2, False)]


def test_other_address_vendor_or_channel_is_another_camera() -> None:
    cfg = AppConfig()
    dev = _device(cfg)
    cam, _ = _add(cfg, dev)
    cfg.walls[0].cells = [None, None, None, cam.id]
    cfg.remove_device(dev.id, now=NOW)
    for host, vendor in (("192.168.1.65", "hikvision"), ("192.168.1.64", "onvif")):
        other = _device(cfg, host=host, vendor=vendor)
        new, placed = _add(cfg, other)
        assert placed == [PlacedCamera(new.id, 1, 0, False)]
        cfg.remove_device(other.id, now=NOW)
    same = _device(cfg)
    new, placed = _add(cfg, same)
    assert placed == [PlacedCamera(new.id, 1, 3, True)]


def test_host_names_match_without_case() -> None:
    cfg = AppConfig()
    dev = _device(cfg, host="Camara-Entrada.local")
    cam, _ = _add(cfg, dev)
    cfg.walls[0].cells = [None, cam.id]
    cfg.remove_device(dev.id, now=NOW)
    assert cfg.wall_memory[0].host == "camara-entrada.local"
    new, placed = _add(cfg, _device(cfg, host="camara-entrada.LOCAL"))
    assert placed == [PlacedCamera(new.id, 1, 1, True)]


def test_memory_expires_after_30_days_and_is_capped() -> None:
    cfg = AppConfig()
    dev = _device(cfg)
    cam, _ = _add(cfg, dev)
    cfg.walls[0].cells = [None, cam.id]
    cfg.remove_device(dev.id, now=NOW)
    later = NOW + timedelta(days=WALL_MEMORY_DAYS, seconds=1)
    again = _device(cfg)
    new, placed = _add(cfg, again, now=later)
    assert placed == [PlacedCamera(new.id, 1, 0, False)], "a los 30 días ya no se recuerda"

    # tope: un grabador que llenaba los 4 muros (64 celdas) y después otro con 10 cámaras
    cfg2 = AppConfig()
    for w in cfg2.walls:
        w.grid = 16
    big = _device(cfg2, host="10.0.0.1", kind="nvr")
    cams = [Camera(name=f"C{i}", device_id=big.id, channel=i) for i in range(1, WALL_MEMORY_MAX + 1)]
    cfg2.cameras += cams
    assert len(cfg2.place_new_cameras(cams, now=NOW)) == 16, "las nuevas solo van al monitor 1"
    for i, c in enumerate(cams):
        cfg2.walls[i // 16].cells[i % 16] = c.id
    cfg2.remove_device(big.id, now=NOW)
    assert len(cfg2.wall_memory) == WALL_MEMORY_MAX
    small = _device(cfg2, host="10.0.0.2", kind="nvr")
    cams2 = [Camera(name=f"D{i}", device_id=small.id, channel=i) for i in range(1, 11)]
    cfg2.cameras += cams2
    assert len(cfg2.place_new_cameras(cams2, now=NOW)) == 10
    cfg2.remove_device(small.id, now=NOW + timedelta(minutes=1))
    assert len(cfg2.wall_memory) == WALL_MEMORY_MAX
    assert {m.host for m in cfg2.wall_memory[-10:]} == {"10.0.0.2"}, "se quedan las más recientes"
    assert min(m.channel for m in cfg2.wall_memory if m.host == "10.0.0.1") == 11, "se van las más antiguas"
    # al cargar, una lista más larga (editada a mano) también se recorta
    doc = json.loads(cfg2.model_dump_json())
    doc["wall_memory"] = doc["wall_memory"] * 3
    assert len(AppConfig.model_validate(doc).wall_memory) == WALL_MEMORY_MAX


def test_damaged_memory_entries_do_not_break_config_json(tmp_path: Path) -> None:
    doc: dict[str, Any] = json.loads(AppConfig().model_dump_json())
    good = {"host": "10.0.0.5", "vendor": "dahua", "channel": 3, "monitor": 2, "cell": 4,
            "removed_at": "2026-10-01T10:00:00"}          # sin zona: se toma como UTC
    doc["wall_memory"] = [good, {"host": "10.0.0.6", "monitor": 9}, "basura", None]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    cfg, warning = ConfigStore(path).load()
    assert warning is None
    assert [m.key() for m in cfg.wall_memory] == [("10.0.0.5", "dahua", 3)]
    assert cfg.wall_memory[0].removed_at.tzinfo is not None
    doc["wall_memory"] = {"no": "es una lista"}
    assert AppConfig.model_validate(doc).wall_memory == []


def test_old_config_without_the_field_loads_and_saves_it(tmp_path: Path) -> None:
    """Un config.json anterior (v1 o v2 sin `wall_memory`) carga con la memoria vacía, sin subir `version`, y al
    guardar ya la incluye; uno que la tiene la conserva tal cual."""
    for name in ("config_v1_tienda.json", "config_v1_tienda.expected_v2.json"):
        raw = json.loads((FIX / name).read_text(encoding="utf-8"))
        assert "wall_memory" not in raw
        path = tmp_path / name
        path.write_text(json.dumps(raw), encoding="utf-8")
        store = ConfigStore(path)
        cfg, warning = store.load()
        assert warning is None and cfg.wall_memory == [] and cfg.version == cm.CONFIG_VERSION == 2
        cam = cfg.camera("cam-22222222")                   # «Caja», en la celda 2 del monitor 1
        assert cam is not None
        cfg.remove_camera(cam.id, now=NOW)
        store.save(cfg)
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["version"] == 2
        assert [(m["host"], m["vendor"], m["channel"], m["monitor"], m["cell"]) for m in saved["wall_memory"]] == [
            ("192.0.2.21", "dahua", 1, 1, 1)]
        reloaded = ConfigStore(path).load()[0]
        assert reloaded.wall_memory == cfg.wall_memory
        dev = reloaded.device("dev-4e5f6a7b")
        assert dev is not None
        new, placed = _add(reloaded, dev)
        assert placed == [PlacedCamera(new.id, 1, 1, True)]


def test_placements_are_plain_data() -> None:
    p = WallPlacement(host=" Cam-Entrada.local ", vendor="hikvision", channel=1, monitor=1, cell=0, removed_at=NOW)
    assert p.key() == ("cam-entrada.local", "hikvision", 1)
    assert PlacedCamera("cam-12345678", 1, 0, False)._asdict() == {"camera_id": "cam-12345678", "monitor": 1,
                                                                    "cell": 0, "restored": False}
