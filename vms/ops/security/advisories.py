"""Tabla de avisos de seguridad propia (CONTRATO §18.12, PLAN-V2 §9.2).

Dos copias y una sola regla:
- **la de la versión**: `vms/ops/security/advisories.json` (viaja dentro de cada versión; la mantiene B6);
- **la que llega entre versiones**: componente TUF `data` que solo escribe `VMSUpdater` en
  `<datos>/ops/advisories/advisories.json`.

Se usa la **válida** (esquema `AdvisoryTable` 1) con el `generated_at` más reciente; con el mismo
`generated_at`, la de la versión. Una copia inválida se ignora con un aviso en el registro. La tabla la
prepara Unmanned en el PC de publicación (NVD API 2.0 + KEV de CISA, CC0 + EPSS de FIRST): ni las tiendas
ni la central salen a Internet.

Comparación: Hikvision por fecha de build (NVD no trae rangos de versión); Dahua por versión numérica por
partes. El firmware Dahua llega como «2.820.0000000.18.R,build:2021-07-05» y la tabla lo escribe como
«2.820.0000000.18.R.210705»: se normaliza a la misma forma (versión + build AAMMDD) antes de comparar, y el
modelo se compara sin los prefijos de marca «DH-»/«DHI-» (`deviceType` real: «DHI-IPC-HFW5442E-ZE»).
Un aviso sin versión o fecha corregida verificada da siempre «Desconocido», nunca «Sin CVE». Resultado: «Vulnerable (KEV)», «Probablemente vulnerable», «Sin CVE conocidos en la tabla» o
«Desconocido». **Nunca «seguro».**
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from vms.core.errors import VmsError

from ..models import Advisory, AdvisoryTable

log = logging.getLogger("vms.ops.security")

Origin = Literal["version", "update"]
_BUILD_RE = re.compile(r"build\s*(\d{6})", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_DAHUA_BUILD_RE = re.compile(r"[,\s]*build\s*:?\s*(\d{4})-?(\d{2})-?(\d{2})", re.IGNORECASE)
_MODEL_PREFIXES = {"dahua": ("DHI-", "DH-")}


@dataclass(frozen=True)
class LoadedTable:
    table: AdvisoryTable
    origin: Origin
    path: str


def _parse(data: bytes | str, where: str) -> AdvisoryTable | None:
    try:
        table = AdvisoryTable.model_validate(json.loads(data))
        for adv in table.advisories:
            re.compile(adv.model_regex)
        return table
    except (ValueError, ValidationError, re.error) as exc:
        log.warning("Tabla de avisos de seguridad no válida en %s: se ignora (%s)", where, str(exc)[:200])
        return None


def version_table() -> LoadedTable:
    ref = resources.files("vms.ops.security").joinpath("advisories.json")
    try:
        text = ref.read_text(encoding="utf-8")
    except FileNotFoundError as exc:   # instalación incompleta (p. ej. wheel sin los datos del paquete)
        raise VmsError("Falta la tabla de avisos de seguridad de esta versión: reinstala el programa",
                       code="install_incomplete", status=503) from exc
    table = _parse(text, "la versión")
    if table is None:   # pragma: no cover - lo impide tests/ops/test_security_audit.py
        raise RuntimeError("La tabla de avisos de la versión no es válida")
    return LoadedTable(table, "version", str(ref))


def update_table_path(data_dir: Path) -> Path:
    return Path(data_dir) / "ops" / "advisories" / "advisories.json"


def load_table(data_dir: Path) -> LoadedTable:
    base = version_table()
    path = update_table_path(data_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return base
    except OSError as exc:
        log.warning("No se pudo leer la tabla de avisos actualizada (%s): se usa la de la versión", exc)
        return base
    upd = _parse(raw, str(path))
    if upd is None or upd.generated_at <= base.table.generated_at:
        return base
    return LoadedTable(upd, "update", str(path))


# --------------------------------------------------------------------------- comparación
def firmware_build_date(firmware: str, firmware_date: str = "") -> date | None:
    """Fecha del build: «2021-06-28» (DeviceInfo.firmware_date) o «V5.5.800 build 210628» del firmware."""
    m = _ISO_DATE_RE.match(firmware_date or "")
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m2 = _BUILD_RE.search(firmware or "") or _BUILD_RE.search(firmware_date or "")
    if m2:
        s = m2.group(1)
        try:
            return date(2000 + int(s[:2]), int(s[2:4]), int(s[4:6]))
        except ValueError:
            return None
    return None


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def dahua_version_tuple(firmware: str, firmware_date: str = "") -> tuple[tuple[int, ...], bool]:
    """(versión normalizada, ¿lleva el build?). «2.820.0000000.18.R,build:2021-07-05» → (2, 820, 0, 18, 210705).

    El build (fecha) va al final como AAMMDD, igual que en la versión corregida de la tabla
    («2.820.0000000.18.R.210705»). Si el firmware no trae build, se usa `firmware_date` si lo hay."""
    fw = firmware or ""
    m = _DAHUA_BUILD_RE.search(fw)
    base = fw[:m.start()] if m is not None else fw
    if m is None and firmware_date:
        m = _DAHUA_BUILD_RE.search("build:" + firmware_date)
    parts = version_tuple(base)
    if m is None:
        return parts, False
    yymmdd = int(f"{int(m.group(1)) % 100:02d}{m.group(2)}{m.group(3)}")
    if parts and parts[-1] == yymmdd:          # ya venía al final («…18.R.210705»)
        return parts, True
    return parts + (yymmdd,), True


def normalize_model(vendor: str, model: str) -> str:
    """Modelo sin el prefijo de marca («DHI-IPC-HFW5442E-ZE» → «IPC-HFW5442E-ZE»)."""
    m = (model or "").strip()
    for prefix in _MODEL_PREFIXES.get(vendor, ()):
        if m.upper().startswith(prefix):
            return m[len(prefix):]
    return m


Verdict = Literal["vulnerable", "probably_vulnerable", "ok", "unknown"]


@dataclass(frozen=True)
class Match:
    advisory: Advisory
    verdict: Verdict
    detail_es: str


def evaluate(table: AdvisoryTable, vendor: str, model: str, firmware: str, firmware_date: str = "") -> list[Match]:
    """Avisos que afectan a un equipo. Sin coincidencias de modelo → lista vacía."""
    out: list[Match] = []
    plain = normalize_model(vendor, model)
    for adv in table.advisories:
        if adv.vendor != vendor or not model or not (re.search(adv.model_regex, model, re.IGNORECASE)
                                                     or re.search(adv.model_regex, plain, re.IGNORECASE)):
            continue
        cves = ", ".join(adv.cves)
        if adv.compare == "build_date":
            built = firmware_build_date(firmware, firmware_date)
            if built is None:
                out.append(Match(adv, "unknown", f"{cves}: no se conoce la fecha del firmware para compararla."))
                continue
            if adv.fixed_build_date is None:
                # sin versión corregida verificada no se puede afirmar nada: nunca «Sin CVE»
                out.append(Match(adv, "unknown", f"{cves}: la tabla no tiene aún la versión corregida verificada; "
                                                 "actualiza el firmware a la última del fabricante."))
                continue
            fixed = date.fromisoformat(adv.fixed_build_date)
            if built >= fixed:
                out.append(Match(adv, "ok", f"{cves}: firmware con build {built.isoformat()} (corregido desde "
                                            f"{fixed.isoformat()})."))
            else:
                out.append(Match(adv, "vulnerable" if adv.families == [] else "probably_vulnerable",
                                 f"{cves}: firmware con build {built.isoformat()}, anterior al que lo corrige "
                                 f"({fixed.isoformat()})."))
        else:
            if vendor == "dahua":
                current, has_build = dahua_version_tuple(firmware, firmware_date)
            else:
                current, has_build = version_tuple(firmware), True
            if not current or not adv.fixed_version:
                out.append(Match(adv, "unknown", f"{cves}: no se puede comparar la versión del firmware."))
                continue
            fixed_v = version_tuple(adv.fixed_version)
            if not has_build and len(current) < len(fixed_v) and current == fixed_v[:len(current)]:
                # misma versión que la corregida pero sin build: no se sabe si es anterior o posterior
                out.append(Match(adv, "unknown", f"{cves}: el firmware {firmware} no indica el build para compararlo "
                                                 f"con {adv.fixed_version}."))
                continue
            if current >= fixed_v:
                out.append(Match(adv, "ok", f"{cves}: versión {firmware} igual o posterior a {adv.fixed_version}."))
            else:
                out.append(Match(adv, "probably_vulnerable" if adv.families else "vulnerable",
                                 f"{cves}: versión {firmware} anterior a {adv.fixed_version}."))
    return out
