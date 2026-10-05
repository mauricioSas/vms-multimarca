"""Registro de drivers: reglas de cada DriverSpec, fachada `rtsp.preset_paths`, detección y «un driver = un archivo»."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from vms.core import rtsp
from vms.core.interfaces import Capability, DetectionHints, DriverSpec, StreamPreset
from vms.core.models import DeviceCreate, known_vendor_ids
from vms.vendors import registry
from vms.vendors.detect import constant
from vms.vendors.registry import REGISTRY, RegistryError, best_match, get_driver, guess_vendor_id, register

ROOT = Path(__file__).resolve().parents[2]
V2_DRIVERS = {"hikvision", "dahua", "onvif", "generic", "ezviz", "imou", "uniview", "tplink-vigi", "tapo", "hanwha",
              "axis", "ajax", "reolink", "bosch"}


def test_plan_matrix_is_registered() -> None:
    """PLAN-V2 §3.4: 3 drivers con API, perfiles RTSP/ONVIF y RTSP manual."""
    assert V2_DRIVERS <= set(REGISTRY)
    api = {s.id for s in REGISTRY.values() if s.client is not None}
    assert api == {"hikvision", "dahua", "onvif"}
    assert REGISTRY["generic"].public().manual_path is True
    assert set(REGISTRY) <= known_vendor_ids()            # el alta acepta todas las marcas del registro
    for vid in REGISTRY:
        assert DeviceCreate(name="x", vendor=vid, host="10.0.0.1").vendor == vid


@pytest.mark.parametrize("spec", list(REGISTRY.values()), ids=lambda s: s.id)
def test_driver_spec_rules(spec: DriverSpec) -> None:
    pub = spec.public()
    json.dumps(pub.model_dump())                          # serializable, sin funciones
    assert pub.name and pub.brands and pub.kinds and pub.auth
    assert all(isinstance(t, str) and t.strip() for t in (*spec.notes_es, *spec.setup_hints_es))
    assert 0.0 <= spec.detect(DetectionHints()) <= 1.0     # pistas vacías no rompen el detector
    if spec.presets is not None:
        for kind in spec.kinds:
            for ch in (1, 2, 16, 33):
                p = spec.presets(ch, kind)
                assert isinstance(p, StreamPreset) and p.main.startswith("/")
                assert ("//" not in p.main) or spec.double_slash
                if "?" in p.main:
                    assert p.query_safe is False, f"{spec.id}: ruta con query sin query_safe=False"
    if Capability.API_CODEC_FIX in spec.capabilities:
        assert spec.client is not None
    if spec.lockout is not None:
        assert spec.lockout.attempts >= 1 and spec.lockout.minutes >= 1
    assert "basic" not in spec.auth[:1] or len(spec.auth) == 1, "Basic nunca como primera opción"


def test_register_rejects_bad_specs() -> None:
    base = dict(name="X", brands=("X",), kinds=("camera",), capabilities=frozenset(), default_ports={"rtsp": 554},
                auth=("digest-md5",), maturity="experimental", detect=constant(0.0))
    with pytest.raises(RegistryError):
        register(DriverSpec(id="Mal ID", **base))  # type: ignore[arg-type]
    with pytest.raises(RegistryError):
        register(DriverSpec(id="hikvision", **base))  # type: ignore[arg-type]   # duplicado
    with pytest.raises(RegistryError):
        register(DriverSpec(id="prueba-x", presets=lambda ch, k: StreamPreset(main="//doble", sub=None),
                            **base))  # type: ignore[arg-type]
    with pytest.raises(RegistryError):
        register(DriverSpec(id="prueba-y", **{**base, "capabilities": frozenset({Capability.API_CODEC_FIX})}))  # type: ignore[arg-type]
    assert get_driver("prueba-x") is None and get_driver("prueba-y") is None


def test_rtsp_facade_delegates_to_registry() -> None:
    assert rtsp.preset_paths("hikvision", 2) == ("/Streaming/Channels/201", "/Streaming/Channels/202")
    assert rtsp.preset_paths("dahua", 5, "xvr") == ("/cam/realmonitor?channel=5&subtype=0",
                                                    "/cam/realmonitor?channel=5&subtype=1")
    assert rtsp.preset_paths("uniview", 3) == ("/unicast/c3/s0/live", "/unicast/c3/s1/live")
    assert rtsp.preset_paths("reolink", 2) == ("/Preview_02_main", "/Preview_02_sub")
    assert rtsp.preset_paths("hanwha", 1, "camera") == ("/profile2/media.smp", "/profile3/media.smp")
    assert rtsp.preset_paths("hanwha", 2, "nvr") == ("/1/profile2/media.smp", "/1/profile3/media.smp")
    assert rtsp.preset_paths("onvif", 1) is None and rtsp.preset_paths("generic", 1) is None
    assert rtsp.preset_paths("no-existe", 1) is None
    with pytest.raises(ValueError):
        rtsp.preset_paths("hikvision", 0)
    assert rtsp.vendor_name("tplink-vigi") == "TP-Link VIGI" and rtsp.vendor_name("x") == "x"


@pytest.mark.parametrize(("hints", "expected"), [
    (DetectionHints(sadp=True, model="DS-2CD2143G2-I"), "hikvision"),
    (DetectionHints(sadp=True, model="CS-C6N-A0-1C2WFR"), "ezviz"),
    (DetectionHints(dhip=True, model="DHI-NVR4208-8P"), "dahua"),
    (DetectionHints(dhip=True, model="IPC-A22EP", manufacturer="Imou"), "imou"),
    (DetectionHints(model="IPC2122LB-SF28-A"), "uniview"),
    (DetectionHints(model="IPC-HDW2431T-AS-S2"), "dahua"),
    (DetectionHints(scopes=["onvif://www.onvif.org/name/UNV", "onvif://www.onvif.org/hardware/IPC-HDW2431T"]), "uniview"),
    (DetectionHints(scopes=["onvif://www.onvif.org/name/AXIS", "onvif://www.onvif.org/hardware/M1065-L"]), "axis"),
    (DetectionHints(model="VIGI C440"), "tplink-vigi"),
    (DetectionHints(scopes=["onvif://www.onvif.org/name/Tapo"], model="C210"), "tapo"),
    (DetectionHints(model="XNO-6080R"), "hanwha"),
    (DetectionHints(model="RLC-510A"), "reolink"),
    (DetectionHints(model="NBN-73023BA"), "bosch"),
    (DetectionHints(scopes=["onvif://www.onvif.org/name/VIVOTEK"], model="FD9389"), "onvif"),
])
def test_best_match(hints: DetectionHints, expected: str) -> None:
    assert guess_vendor_id(hints)[0] == expected, best_match(hints)[:3]


def test_scopes_weigh_more_than_model_prefix() -> None:
    """«IPC-» ya no implica Dahua: el scope `name/` del equipo manda sobre el prefijo del modelo."""
    hints = DetectionHints(scopes=["onvif://www.onvif.org/name/HIKVISION"], model="IPC-HDW1000")
    top = best_match(hints)[0]
    assert top[0].id == "hikvision"


def test_broken_detector_does_not_break_matching(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_h: DetectionHints) -> float:
        raise RuntimeError("detector roto")
    spec = REGISTRY["bosch"]
    monkeypatch.setitem(REGISTRY, "bosch", DriverSpec(**{**spec.__dict__, "detect": boom}))
    assert best_match(DetectionHints(sadp=True))[0][0].id == "hikvision"


def test_adding_a_driver_needs_no_other_file() -> None:
    """Ningún archivo fuera de `vms/vendors/drivers/<id>.py` (y pruebas/docs) nombra los ids de la v2: ni la API,
    ni el motor, ni la interfaz. Los de la v1 se toleran solo donde ya estaban (compatibilidad)."""
    v1 = {"hikvision", "dahua", "onvif", "generic"}
    sources = [p for p in (ROOT / "vms").rglob("*.py") if "drivers" not in p.parts]
    sources += list((ROOT / "vms" / "web" / "static" / "js").glob("*.js"))
    def code(p: Path) -> str:   # sin comentarios de línea (los docstrings pueden citar marcas)
        text = p.read_text(encoding="utf-8")
        return re.sub(r"(?m)#.*$", "", text) if p.suffix == ".py" else re.sub(r"(?m)//.*$", "", text)

    for vid in set(REGISTRY) - v1:
        pattern = re.compile(rf"""["']{re.escape(vid)}["']""")
        hits = [str(p.relative_to(ROOT)) for p in sources if pattern.search(code(p))]
        assert hits == [], f"«{vid}» aparece fuera de su driver: {hits}"
    js = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "vms" / "web" / "static" / "js").glob("*.js"))
    # sin presets duplicados en JS (PLAN-V2 §6.2 B5, criterio 3)
    for marker in ("Streaming/Channels", "realmonitor", "/unicast/", "Preview_", "media.smp", "axis-media",
                   "presetPaths"):
        assert marker not in js, marker


def test_load_drivers_is_idempotent() -> None:
    before = dict(REGISTRY)
    registry.load_drivers()
    assert REGISTRY == before
