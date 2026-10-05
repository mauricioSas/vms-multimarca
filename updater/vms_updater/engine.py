"""Lógica del actualizador: comprobar, descargar, aplicar con diario, verificar y volver atrás
(PLAN-V2 §2.5, CONTRATO §13.4-§13.5 y §15).

Garantía central: pase lo que pase (corte de luz, proceso matado, disco lleno, versión rota), al volver a
arrancar el resultado es **«nueva buena» o «anterior buena»**, nunca un estado intermedio, y ejecutar la
recuperación dos veces da lo mismo (cada paso es idempotente). El punto de compromiso es `switched`: antes,
la recuperación revierte; después, sigue adelante y, si el health check falla, vuelve atrás.
"""
from __future__ import annotations

import errno
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from . import __version__
from ._atomic import atomic_write_json, cleanup_temporaries, read_json
from .advisories import install_advisories, installed_generated_at, latest_advisories_target
from .authenticode import AuthenticodeError, Verifier, verify_tree
from .backup import backup_name, create_backup, prune_backups, restore_config
from .client import DiskFull, MetadataExpired, NotAuthorized, SecurityError, TufClient, UpdateSourceError
from .health import HealthResult
from .journal import FaultHook, JournalStore
from .layout import Layout
from .migrate import MigrationError
from .models import (FORWARD_STATES, KNOWN_SERVICES, ChannelDoc, Journal, LocalUpdaterConfig, ReleaseDescriptor,
                     bundle_target, channel_target, iso, utcnow)
from .pointer import PointerStore, rebuild_pointer
from .services import ServiceControl, ServiceError
from .stage import StageError, install_updater_slot, remove_tree, stage_version, verify_staged
from .state_files import (Blacklist, InstallLock, StatusFile, VersionMarks, in_window, read_directive,
                          read_local_config, write_local_config)
from .system import SystemInfo
from .versioning import Version

log = logging.getLogger("vms_updater.engine")

MAX_CLOCK_SKEW_S = 300.0
MAX_ATTEMPTS = 3
RELOAD_EXIT_CODE = 3        # «vuelve a leer active.json y relánzame» (actualizador A/B, petición a B1)
DISK_MARGIN = 1024 ** 3     # 1 GB además de 2× el tamaño de la versión


class HealthChecker(Protocol):
    def recording_now(self) -> int | None: ...
    def wait_healthy(self, expected_version: str | None, min_recording: int | None) -> HealthResult: ...


@dataclass
class Outcome:
    result: str
    message_es: str = ""
    available: str | None = None
    applied: str | None = None


@dataclass
class EngineDeps:
    layout: Layout
    client_factory: Callable[[], TufClient]
    services: ServiceControl
    health: HealthChecker
    system: SystemInfo
    migrate: Callable[..., None]
    verifier: Verifier | None = None
    dev_root: bool = False
    require_authenticode: bool = False
    clock: Callable[[], float] = time.time
    now_utc: Callable[[], datetime] = utcnow
    now_local: Callable[[], datetime] = field(default_factory=lambda: (lambda: datetime.now().astimezone()))
    fault_hook: FaultHook | None = None
    slot: str | None = None            # ranura en la que corre este proceso ("a"/"b"); None = desconocida
    max_clock_skew_s: float = MAX_CLOCK_SKEW_S
    on_event: Callable[[str, dict[str, Any]], None] | None = None


class Engine:
    def __init__(self, deps: EngineDeps) -> None:
        self.d = deps
        L = deps.layout
        self.layout = L
        self.pointer = PointerStore(L.pointer_file, clock=deps.clock)
        self.journal = JournalStore(L.journal_file, clock=deps.clock, fault_hook=deps.fault_hook)
        self.status = StatusFile(L.public_status_file, now=deps.now_utc)
        self.blacklist = Blacklist(L.blacklist_file)
        self.lock = InstallLock(L.lock_file, clock=deps.clock)
        self.marks = VersionMarks(L.versions_state_file)
        # Dos cerrojos (siempre en este orden): `_op` serializa comprobaciones y órdenes; `_apply` solo se
        # toma mientras se TOCA la instalación (pasos del diario, vuelta atrás, ranura del actualizador). El
        # instalador recibe «busy» únicamente con `_apply` cogido, no durante una descarga larga.
        self._op = threading.RLock()
        self._apply = threading.RLock()

    # ================================================================== utilidades
    def config(self) -> LocalUpdaterConfig:
        return read_local_config(self.layout.local_config_file)

    def installed(self) -> str | None:
        p = self.pointer.read()
        return p.active if p else None

    def descriptor_of(self, version: str | None) -> ReleaseDescriptor | None:
        if not version:
            return None
        raw = read_json(self.layout.version_dir(version) / "release.json")
        if not isinstance(raw, dict):
            return None
        try:
            return ReleaseDescriptor.model_validate(raw)
        except ValidationError:
            log.warning("release.json de %s no es válido", version)
            return None

    def _installed_services(self) -> list[str]:
        return [s for s in self.config().services if s in KNOWN_SERVICES and s != "VMSUpdater"]

    def affected_services(self, desc: ReleaseDescriptor, changed: list[str]) -> list[str]:
        wanted: list[str] = []
        for c in changed:
            ref = desc.components.get(c)
            if ref is None or c == "updater":
                continue
            for s in ref.restart:
                if s not in wanted and s != "VMSUpdater":
                    wanted.append(s)
        installed = set(self._installed_services())
        return [s for s in KNOWN_SERVICES if s in wanted and s in installed]

    def changed_components(self, new: ReleaseDescriptor, cur: ReleaseDescriptor | None) -> list[str]:
        out = []
        for name, ref in new.components.items():
            if name == "updater":
                continue
            old = cur.components.get(name) if cur else None
            if old is None or old.sha256 != ref.sha256:
                out.append(name)
        return out

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self.d.on_event is not None:
            try:
                self.d.on_event(kind, data)
            except Exception:  # noqa: BLE001
                log.exception("Suscriptor de eventos roto")

    def _set_status(self, **kw: Any) -> None:
        try:
            self.status.update(installed=self.installed(), updater_version=__version__, **kw)
        except OSError as exc:
            log.error("No se pudo escribir public-status.json: %s", exc)

    def announce_pause(self, state: str) -> None:
        self._set_status(paused_at=state, message_es=f"PRUEBA: en pausa en «{state}»")

    # ================================================================== arranque
    def startup(self) -> Outcome | None:
        """Al arrancar el servicio: temporales, puntero, ranura A/B y diario (retomar o revertir)."""
        L = self.layout.ensure()
        for d in (L.state_dir, L.updater_data):
            cleanup_temporaries(d)
        with self._op, self._apply:
            self._ensure_pointer()
            out = self._recover_updater_slot()
            rec = self.recover()
            return rec or out

    def _ensure_pointer(self) -> None:
        if self.pointer.read() is not None:
            return
        j = self.journal.read()
        known: list[str | None] = []
        try:
            known.append(self.d.system.installed_version())
        except OSError:
            pass
        known += self.marks.good()
        ptr = rebuild_pointer(j, self.layout.versions_dir, clock=self.d.clock, known_good=known,
                              unverified=self.marks.pending())
        if ptr is None:
            log.error("No hay puntero ni versiones instaladas: no se puede reconstruir active.json")
            return
        self.pointer.write(ptr)
        log.warning("active.json faltaba o estaba dañado: reconstruido con %s%s", ptr.active,
                    " (a prueba)" if ptr.trial else "")

    # ================================================================== recuperación del diario
    def recover(self) -> Outcome | None:
        with self._op, self._apply:
            j = self.journal.read()
            if j is None or not j.in_progress:
                return None
            log.warning("Diario a medias (%s, estado %s): se retoma", j.update_id, j.state)
            if j.kind == "updater":
                return self._recover_updater_journal(j)
            if j.kind == "rollback":
                return self._manual_rollback_steps(j)
            if j.state in ("rolling_back", "rolled_back"):
                if j.aborted:
                    return self._abort(j, j.error or "interrumpida")
                return self._rollback(j, j.error or "vuelta atrás interrumpida", blacklist=True)
            ptr = self.pointer.read()
            committed = ptr is not None and j.to is not None and ptr.active == j.to
            if not committed:
                return self._abort(j, "la actualización se interrumpió antes de cambiar de versión")
            j = self.journal.set(j, attempt=j.attempt + 1)
            if j.attempt > MAX_ATTEMPTS:
                return self._rollback(j, f"la actualización se interrumpió {j.attempt - 1} veces", blacklist=True)
            return self._forward_from(j)

    def _last_done_index(self, j: Journal) -> int:
        idx = -1
        for s in j.steps:
            if s.done_unix is not None and s.state in FORWARD_STATES:
                idx = max(idx, FORWARD_STATES.index(s.state))
        return idx

    # ================================================================== comprobación
    def check(self, *, force_window: bool = False, apply: bool = True) -> Outcome:
        with self._op:
            try:
                return self._check(force_window=force_window, apply=apply)
            except Exception as exc:  # noqa: BLE001 - el servicio nunca se cae por una comprobación
                log.exception("Error inesperado en la comprobación")
                self._set_status(last_result="error", message_es=f"Error inesperado: {type(exc).__name__}")
                return Outcome("error", f"Error inesperado: {type(exc).__name__}")

    def _check(self, *, force_window: bool, apply: bool) -> Outcome:
        if self.recover() is not None:
            pass
        now = self.d.now_utc()
        cfg = self._apply_directive(self.config())
        self._set_status(last_check=iso(now), channel=cfg.channel, hold=cfg.hold, window=cfg.window,
                         skipped=self.blacklist.skipped(), state=self._journal_state(), paused_at=None)
        client = self.d.client_factory()
        try:
            info = client.refresh()
        except MetadataExpired as exc:
            # Con el reloj adelantado más que la validez del timestamp, TUF ve los metadatos caducados: si la
            # cabecera Date del servidor dice otra cosa, el problema es el reloj, no el servidor.
            skew = self._skew(now, getattr(client.fetcher, "last_server_date", None))
            if skew is not None and abs(skew) > self.d.max_clock_skew_s:
                self._set_status(clock_skew_s=round(skew, 1))
                return self._finish("clock_skew", self._skew_message(skew))
            return self._finish("metadata_expired", exc.message_es)
        except (SecurityError, NotAuthorized, UpdateSourceError) as exc:
            return self._finish("error", exc.message_es)
        expires = iso(info.timestamp_expires)
        skew = self._skew(now, info.server_date)
        self._set_status(metadata_expires=expires, clock_skew_s=round(skew, 1) if skew is not None else None,
                         reboot_pending=self.d.system.reboot_pending())
        if skew is not None and abs(skew) > self.d.max_clock_skew_s:
            return self._finish("clock_skew", self._skew_message(skew))
        # Primera comprobación TUF correcta del actualizador nuevo (A/B): ahora sí se confirma su ranura.
        self._confirm_updater_if_pending()
        self._update_advisories(client)
        return self._select_and_apply(client, cfg, force_window=force_window, apply=apply)

    @staticmethod
    def _skew(now: datetime, server_date: datetime | None) -> float | None:
        return (now - server_date).total_seconds() if server_date is not None else None

    @staticmethod
    def _skew_message(skew: float) -> str:
        if abs(skew) >= 86400:
            amount = f"{skew / 86400:+.1f} días"
        else:
            amount = f"{skew:+.0f} s"
        return (f"El reloj del equipo está desfasado {amount} respecto al servidor de actualizaciones: no se "
                "aplica nada hasta corregirlo (revisa NTP).")

    def _journal_state(self) -> str:
        j = self.journal.read()
        return j.state if j else "idle"

    def _finish(self, result: str, message: str, **kw: Any) -> Outcome:
        log.info("Resultado de la comprobación: %s — %s", result, message)
        self._set_status(last_result=result, message_es=message, state=self._journal_state(), **kw)
        return Outcome(result, message, kw.get("available"))

    def _apply_directive(self, cfg: LocalUpdaterConfig) -> LocalUpdaterConfig:
        """Canal y retención que pide el panel central (CONTRATO §15.6). Se guardan en updater.json."""
        d = read_directive(self.layout.directive_file)
        if d is None:
            return cfg
        changes: dict[str, Any] = {}
        if d.channel and d.channel != cfg.channel:
            changes["channel"] = d.channel
        if d.hold is not None and d.hold != cfg.hold:
            changes["hold"] = d.hold
        if d.window and d.window != cfg.window:
            changes["window"] = d.window
        if changes:
            try:
                cfg = LocalUpdaterConfig.model_validate({**cfg.model_dump(), **changes})
                write_local_config(self.layout.local_config_file, cfg)
                log.info("Ajustes del panel central aplicados: %s", changes)
            except ValidationError:
                log.warning("Ajustes del panel central no válidos: %s", changes)
        return cfg

    def _update_advisories(self, client: TufClient) -> None:
        try:
            latest = latest_advisories_target(client.targets())
            if latest is None:
                return
            name, gen = latest
            have = installed_generated_at(self.layout.advisories_file)
            if have is not None and have >= gen:
                return
            install_advisories(client.read(name), self.layout.advisories_file)
        except (UpdateSourceError, ValueError, OSError) as exc:
            log.warning("No se pudo actualizar la tabla de avisos: %s", exc)

    def _select_and_apply(self, client: TufClient, cfg: LocalUpdaterConfig, *, force_window: bool,
                          apply: bool) -> Outcome:
        installed = self.installed()
        try:
            ch = ChannelDoc.model_validate_json(client.read(channel_target(cfg.channel)))
        except SecurityError:
            return self._finish("error", f"El canal «{cfg.channel}» no está publicado")
        except (ValidationError, ValueError) as exc:
            return self._finish("error", f"Canal «{cfg.channel}» con formato no válido: {exc}")
        except UpdateSourceError as exc:
            return self._finish("error", exc.message_es)
        if ch.channel != cfg.channel:
            return self._finish("error", "El canal firmado no coincide con el pedido")
        if ch.paused:
            return self._finish("no_update", f"El canal «{cfg.channel}» está en pausa", available=None)
        cand = ch.version
        if installed and not Version.parse(cand) > Version.parse(installed):
            return self._finish("no_update", f"Al día ({installed})", available=None)
        if self.blacklist.blocks(cand):
            if self.blacklist.kind_of(cand) == "manual":
                return self._finish("no_update", f"La {cand} se dejó de lado al volver atrás a mano: no se "
                                    "reinstala sola hasta que haya una versión mayor o se pida «Permitir de "
                                    "nuevo»", available=cand)
            return self._finish("no_update", f"La {cand} falló antes en este equipo y no se reintenta "
                                "hasta que haya una versión mayor", available=None)
        try:
            raw = client.read(bundle_target(cand))
            desc = ReleaseDescriptor.model_validate_json(raw)
        except SecurityError as exc:
            return self._finish("error", exc.message_es)
        except (ValidationError, ValueError) as exc:
            return self._finish("error", f"Descriptor de la {cand} no válido: {exc}")
        except UpdateSourceError as exc:
            return self._finish("error", exc.message_es)
        if desc.version != cand:
            return self._finish("error", "El descriptor no corresponde a la versión del canal")
        if desc.min_from and installed and Version.parse(installed) < Version.parse(desc.min_from):
            return self._finish("min_from", f"La {cand} necesita tener instalada al menos la {desc.min_from} "
                                f"(hay {installed}): hace falta el instalador completo", available=cand)
        build = self.d.system.windows_build()
        if desc.requires.windows_build_min and build is not None and build < desc.requires.windows_build_min:
            return self._finish("error", f"La {cand} necesita Windows build {desc.requires.windows_build_min} "
                                f"o superior (este equipo: {build})", available=cand)
        if desc.authenticode is None and self.d.require_authenticode:
            return self._finish("error", f"La {cand} no está firmada (Authenticode) y este equipo lo exige",
                                available=cand)
        if cfg.hold:
            return self._finish("held", f"Hay una versión nueva ({cand}), pero las actualizaciones de esta sede "
                                "están retenidas desde el panel", available=cand)
        if not apply:
            return self._finish("no_update", f"Disponible la {cand}", available=cand)
        # ---- descarga y montaje (fuera de la ventana; no corta nada)
        cur_desc = self.descriptor_of(installed)
        changed = self.changed_components(desc, cur_desc)
        try:
            self._download_and_stage(client, desc, raw, changed, installed, cur_desc)
        except _DiskFull as exc:
            return self._finish("disk_full", exc.message_es, available=cand)
        except (StageError, AuthenticodeError) as exc:
            self.blacklist.add(cand, exc.message_es)
            return self._finish("update_failed", f"La {cand} no supera la verificación: {exc.message_es}",
                                available=cand)
        except SecurityError as exc:
            return self._finish("error", exc.message_es, available=cand)
        except UpdateSourceError as exc:
            return self._finish("error", exc.message_es, available=cand)
        # ---- ¿se puede aplicar ya?
        touches_engine = "engine" in changed
        urgent = desc.security and desc.severity == "critical" and not touches_engine
        if not (force_window or urgent or in_window(self.d.now_local(), cfg.window)):
            return self._finish("waiting_window", f"La {cand} está descargada y se aplicará en la ventana de "
                                f"mantenimiento ({cfg.window})", available=cand)
        if self.d.system.reboot_pending():
            return self._finish("reboot_pending", "Windows tiene un reinicio pendiente: la actualización espera a "
                                "la siguiente ventana", available=cand, reboot_pending=True)
        with self._apply:
            # Comprobado con `_apply` cogido: el instalador no puede coger el cerrojo entre esto y aplicar.
            holder = self.lock.holder()
            if holder is not None:
                return self._finish("waiting_window", f"Hay otra instalación en curso ({holder}): se espera",
                                    available=cand)
            if changed:
                out = self.apply_release(desc, changed)
            else:
                out = Outcome("update_ok", "", cand, cand)
            if out.result == "update_ok":
                up = self._maybe_update_updater(client, desc)
                if up is not None:
                    return up
            return out

    # ================================================================== descarga y montaje
    def _download_and_stage(self, client: TufClient, desc: ReleaseDescriptor, raw: bytes, changed: list[str],
                            installed: str | None, cur_desc: ReleaseDescriptor | None) -> None:
        total = sum(desc.components[c].length for c in changed)
        biggest = max((desc.components[c].length for c in changed), default=0)
        # versions\ (2× + margen), la caché de descargas en <datos> (1×) y TEMP, donde ngclient descarga cada
        # componente antes de copiarlo a la caché (el mayor). Pueden ser volúmenes distintos.
        needs = [(self.layout.install, 2 * total + DISK_MARGIN, "la carpeta de instalación"),
                 (self.layout.tuf_targets_dir, total, "la caché de descargas"),
                 (Path(tempfile.gettempdir()), biggest, "la carpeta temporal")]
        for where, need, label in needs:
            free = self._free(where)
            if free is not None and free < need:
                raise _DiskFull(f"No hay espacio suficiente en {label}: hacen falta {need / 1e9:.1f} GB y quedan "
                                f"{free / 1e9:.1f} GB")
        zips: dict[str, Path] = {}
        for c in changed:
            ref = desc.components[c]
            try:
                path = client.download(ref.target)
            except DiskFull as exc:
                raise _DiskFull(exc.message_es) from exc
            tf = client.targets().get(ref.target)
            if tf is None or tf.hashes.get("sha256") != ref.sha256 or tf.length != ref.length:
                raise SecurityError(f"El descriptor y targets.json no coinciden para «{c}»")
            zips[c] = path
        try:
            self.marks.mark_pending(desc.version)       # antes de que exista: nunca se activa sin comprobar
            vdir = stage_version(versions_dir=self.layout.versions_dir, descriptor=desc, descriptor_bytes=raw,
                                 zips=zips, current_version=installed, current_descriptor=cur_desc)
        except OSError as exc:
            remove_tree(self.layout.staging_dir(desc.version))
            if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)) or getattr(exc, "winerror", 0) in (39, 112):
                raise _DiskFull("Disco lleno al preparar la versión: se reintentará en el siguiente ciclo") from exc
            raise StageError(f"Error de disco al preparar la versión: {exc}") from exc
        self._check_authenticode(vdir, desc, changed)

    def _free(self, path: Path) -> int | None:
        p = Path(path)
        while not p.exists() and p.parent != p:
            p = p.parent
        try:
            return self.d.system.free_bytes(p)
        except OSError:
            return None

    def _check_authenticode(self, vdir: Path, desc: ReleaseDescriptor, changed: list[str]) -> None:
        pol = desc.authenticode
        if pol is None:
            log.warning("La %s no lleva política Authenticode (versión de desarrollo sin firmar)", desc.version)
            return
        if self.d.verifier is None:
            log.warning("Sin verificador Authenticode en esta plataforma: solo cuenta TUF")
            return
        files: list[str] = []
        for c in changed:
            mf = vdir / "manifests" / f"{c}.sha256"
            from .stage import parse_manifest
            files += list(parse_manifest(mf.read_text(encoding="utf-8")))
        try:
            n = verify_tree(vdir, files, self.d.verifier, subject_o=pol.subject_o, subject_c=pol.subject_c,
                            issuers=pol.issuers, third_party=pol.third_party)
        except AuthenticodeError:
            remove_tree(vdir)
            raise
        log.info("Authenticode: %d binarios de la %s comprobados", n, desc.version)

    # ================================================================== aplicar
    def apply_release(self, desc: ReleaseDescriptor, changed: list[str]) -> Outcome:
        with self._op, self._apply:
            installed = self.installed()
            prev_journal = self.journal.read()
            attempt = 1
            if prev_journal and prev_journal.to == desc.version and prev_journal.kind == "release":
                attempt = prev_journal.attempt + 1
            if attempt > MAX_ATTEMPTS:
                self.blacklist.add(desc.version, "demasiados intentos interrumpidos")
                return self._finish("update_failed", f"La {desc.version} se interrumpió {attempt - 1} veces: "
                                    "no se reintenta", available=desc.version)
            now = self.d.clock()
            j = Journal(update_id="u-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)), kind="release",
                        from_=installed, to=desc.version, components=changed,
                        services=self.affected_services(desc, changed), state="idle", attempt=attempt,
                        last_good=installed or (prev_journal.last_good if prev_journal else None))
            self.journal.write(j)
            log.info("Aplicando %s → %s (%s; servicios %s)", installed, desc.version, ",".join(changed),
                     ",".join(j.services) or "ninguno")
            self._set_status(state="downloaded", message_es=f"Aplicando la {desc.version}", available=desc.version)
            return self._forward_from(j)

    def _forward_from(self, j: Journal) -> Outcome:
        """Ejecuta los pasos que falten desde el último hecho. Todos son idempotentes."""
        assert j.to is not None
        start = self._last_done_index(j) + 1
        commit = FORWARD_STATES.index("switched")
        for i, state in enumerate(FORWARD_STATES[start:], start):
            j = self.journal.begin(j, state)
            self._set_status(state=state)
            try:
                j = self._do_step(j, state)
            except _StepFailed as exc:
                if i < commit:          # el puntero sigue en la versión anterior: revertir sin culpar a la nueva
                    return self._abort(j, exc.message_es)
                return self._rollback(j, exc.message_es, blacklist=True)
            j = self.journal.done(j)
        return self._finish("update_ok", f"Actualizado a la {j.to}", available=None)

    def _do_step(self, j: Journal, state: str) -> Journal:
        to = j.to
        assert to is not None
        if state == "downloaded":
            vdir = self.layout.version_dir(to)
            if not (vdir / "release.json").is_file():
                raise _StepFailed(f"versions/{to} no está montada")
            try:
                verify_staged(vdir)       # p. ej. archivos con ceros tras un corte de luz al montarla
            except StageError as exc:
                if to != self.installed():
                    remove_tree(vdir)     # se vuelve a montar en el siguiente ciclo
                raise _StepFailed(f"versions/{to} está dañada ({exc.message_es}): se volverá a montar") from exc
            return j
        if state == "backed_up":
            rec = j.recording_before if j.recording_before is not None else self.d.health.recording_now()
            name = j.backup or backup_name(to, self.d.clock())
            create_backup(data_dir=self.layout.data, backups_dir=self.layout.backups_dir,
                          name=Path(name).name, extra={"from": j.from_, "to": to})
            return self.journal.set(j, backup=f"backups/{Path(name).name}", recording_before=rec)
        if state == "stopping":
            try:
                self.d.services.stop(j.services)
            except ServiceError as exc:
                raise _StepFailed(f"no se pudieron parar los servicios: {exc.message_es}") from exc
            return j
        if state == "switched":
            self.pointer.switch(to, trial=True)
            return j
        if state == "migrated":
            try:
                self.d.migrate(to, central="VMSCentral" in j.services)
            except MigrationError as exc:
                raise _StepFailed(exc.message_es) from exc
            return j
        if state == "started":
            try:
                self._start(j.services, to)
            except ServiceError as exc:
                raise _StepFailed(f"no arrancaron los servicios: {exc.message_es}") from exc
            return j
        if state == "verifying":
            res = self.d.health.wait_healthy(self._expected(j.services, to), j.recording_before)
            if not res.ok:
                raise _StepFailed(res.reason_es)
            return j
        if state == "good":
            self.pointer.confirm()
            self.marks.mark_good(to)
            self._registry(to)
            self._cleanup_versions()
            prune_backups(self.layout.backups_dir)
            self._emit("update", {"version": to, "viewer_restart": "viewer" in j.components, "state": "good",
                                  "message_es": f"Actualizado a la {to}"})
            return self.journal.set(j, last_good=to, error="")
        raise AssertionError(state)

    def _registry(self, version: str) -> None:
        try:
            self.d.system.write_installed_version(version, self.config().inno_app_id)
        except OSError as exc:
            log.error("No se pudo escribir la versión en el registro: %s", exc)

    # ---- qué versión ejecuta cada servicio (un servicio que no se reinicia sigue en su carpeta)
    def _running_file(self) -> Path:
        return self.layout.updater_data / "running.json"

    def _running(self) -> dict[str, str]:
        raw = read_json(self._running_file())
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
        ptr = self.pointer.read()
        return {s: ptr.active for s in self._installed_services()} if ptr else {}

    def _start(self, services: list[str], version: str | None) -> None:
        self.d.services.start(services)
        if version:
            run = self._running()
            run.update({s: version for s in services})
            atomic_write_json(self._running_file(), run)

    @staticmethod
    def _expected(services: list[str], version: str | None) -> str | None:
        """La versión que tiene que decir el backend: solo si se ha reiniciado (si no, sigue la suya)."""
        return version if "VMSBackend" in services else None

    def _cleanup_versions(self) -> None:
        ptr = self.pointer.read()
        keep = {v for v in ((ptr.active, ptr.previous) if ptr else ()) if v}
        if not keep:
            return
        keep |= set(self._running().values())        # nunca la carpeta de un servicio en marcha
        for p in self.layout.versions_dir.iterdir():
            if not p.is_dir() or p.name in keep:
                continue
            try:
                if not remove_tree(p):
                    log.info("No se pudo borrar %s (en uso): se reintentará", p.name)
            except OSError as exc:
                log.info("No se pudo borrar %s (%s): se reintentará", p.name, exc)

    # ================================================================== revertir / volver atrás
    def _abort(self, j: Journal, reason: str) -> Outcome:
        """Antes del punto de compromiso: la versión activa sigue siendo la anterior. Se arrancan los
        servicios por si se pararon y se cierra el diario (sin lista negra: se reintentará)."""
        if not (j.state == "rolled_back" and j.aborted):
            try:
                self._start(j.services, self.installed())
            except ServiceError as exc:
                log.error("No se pudieron arrancar los servicios al revertir: %s", exc.message_es)
            j = self.journal.begin(j.model_copy(update={"error": reason, "aborted": True}), "rolled_back")
        j = self.journal.done(j)
        return self._finish("update_failed", f"Actualización a la {j.to} interrumpida ({reason}); sigue la "
                            f"{self.installed()} y se reintentará", available=j.to)

    def _revert_steps(self, j: Journal, target: str | None, reason: str, *, min_recording: int | None) -> Journal:
        """Paso `rolling_back` (idempotente): parar, puntero a `target` sin prueba, restaurar config, arrancar
        y comprobar. Si el diario ya lo tiene hecho, no se repite."""
        if j.state == "rolled_back" or (j.state == "rolling_back" and self.journal.step_done(j, "rolling_back")):
            return j
        if j.state != "rolling_back":
            j = self.journal.begin(j.model_copy(update={"error": reason}), "rolling_back")
        self._set_status(state="rolling_back", message_es=f"Volviendo a la {target}: {reason}")
        try:
            self.d.services.stop(j.services)
        except ServiceError as exc:
            log.error("No se pudieron parar los servicios: %s", exc.message_es)
        if target and (self.layout.version_dir(target) / "release.json").is_file():
            self.pointer.switch(target, trial=False)
        if j.backup and (self.layout.data / j.backup).is_dir():
            try:
                restore_config(data_dir=self.layout.data, backup_dir=self.layout.data / j.backup)
            except OSError as exc:
                log.error("No se pudo restaurar la configuración: %s", exc)
        try:
            self._start(j.services, target)
        except ServiceError as exc:
            log.error("No arrancaron los servicios de la %s: %s", target, exc.message_es)
        res = (self.d.health.wait_healthy(self._expected(j.services, target), min_recording) if target
               else HealthResult(False, "sin versión"))
        if not res.ok:
            log.critical("La %s no supera el health check tras volver atrás: %s", target, res.reason_es)
        return self.journal.done(j)

    def _rollback(self, j: Journal, reason: str, *, blacklist: bool) -> Outcome:
        """Después del punto de compromiso: volver a la versión anterior y no reintentar la nueva."""
        prev = j.from_ or j.last_good
        bad = j.to
        log.error("Volviendo a la %s: %s", prev, reason)
        j = self._revert_steps(j, prev, reason, min_recording=j.recording_before)
        if blacklist and bad:
            self.blacklist.add(bad, j.error or reason)   # antes de cerrar el diario: un corte aquí no la olvida
        if j.state != "rolled_back":
            j = self.journal.begin(j, "rolled_back")
        if prev:
            self.marks.mark_good(prev)
            self._registry(prev)
        self._emit("update", {"version": prev or "", "viewer_restart": "viewer" in j.components,
                              "state": "rolled_back", "message_es": f"Se volvió a la {prev}: {j.error or reason}"})
        j = self.journal.done(j.model_copy(update={"last_good": prev or j.last_good}))
        return self._finish("update_failed", f"La {bad} falló ({j.error or reason}): se volvió a la {prev}",
                            available=None)

    def manual_rollback(self, to: str | None = None, reason: str = "") -> Outcome:
        """Rollback pedido (tubería elevada, panel central o rollback-request de vmshost).

        La versión de la que se vuelve queda **omitida** (`Blacklist`, `kind="manual"`): si no, el ciclo
        siguiente la volvería a instalar sola. Se levanta con una versión mayor o con `unskip`."""
        with self._op, self._apply:
            self.recover()
            ptr = self.pointer.read()
            if ptr is None:
                return Outcome("error", "No hay versión activa")
            target = to or ptr.previous
            if not target:
                return Outcome("error", "No hay versión anterior a la que volver")
            if target == ptr.active:
                return Outcome("error", f"La {target} ya es la versión activa")
            if not (self.layout.version_dir(target) / "release.json").is_file():
                return Outcome("error", f"La {target} no está instalada en este equipo")
            bdir = self._latest_backup_for(ptr.active)
            j = Journal(update_id="r-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.d.clock())),
                        kind="rollback", from_=ptr.active, to=target, components=[],
                        services=self._installed_services(), state="idle", attempt=1, last_good=ptr.active,
                        backup=f"backups/{bdir.name}" if bdir else None, reason=reason[:300])
            self.journal.write(j)
            return self._manual_rollback_steps(j)

    def _manual_rollback_steps(self, j: Journal) -> Outcome:
        """Volver a `to` (ya instalada y buena). La versión de la que se vuelve queda omitida (no «fallida»):
        se anota ANTES de tocar nada, así un corte a mitad no la olvida (paso idempotente)."""
        target = j.to
        assert target is not None
        left = j.from_
        if left and left != target:
            self.blacklist.add(left, j.reason or "vuelta atrás manual", kind="manual")
        j = self._revert_steps(j, target, j.reason or "vuelta atrás pedida", min_recording=None)
        if j.state != "rolled_back":
            j = self.journal.begin(j, "rolled_back")
        self.marks.mark_good(target)
        self._registry(target)
        self._emit("update", {"version": target, "viewer_restart": True, "state": "rolled_back",
                              "message_es": f"Se volvió a la {target}"})
        j = self.journal.done(j.model_copy(update={"last_good": target}))
        skipped = self.blacklist.skipped()
        note = (f"; la {left} no se reinstalará sola hasta que haya una versión mayor o se pida «Permitir de "
                "nuevo»") if left and left in skipped else ""
        return self._finish("rollback_ok", f"Se volvió a la {target}" + (f" ({j.reason})" if j.reason else "")
                            + note, skipped=skipped)

    def unskip(self, version: str | None = None) -> list[str]:
        """Vuelve a permitir las versiones omitidas tras una vuelta atrás manual (todas o una)."""
        with self._op:
            gone = self.blacklist.unskip(version)
            self._set_status(skipped=self.blacklist.skipped())
            if gone:
                log.info("Versiones permitidas de nuevo: %s", ", ".join(gone))
            return gone

    def _latest_backup_for(self, version: str) -> Path | None:
        d = self.layout.backups_dir
        if not d.is_dir():
            return None
        cands = sorted((p for p in d.glob(f"pre-{version}-*") if p.is_dir() and (p / "backup.json").is_file()),
                       key=lambda p: p.name, reverse=True)
        return cands[0] if cands else None

    def handle_rollback_request(self) -> Outcome | None:
        """`state\\rollback-request.json` de un vmshost sin permiso de escribir el puntero (CONTRATO §13.3)."""
        f = self.layout.rollback_request_file
        raw = read_json(f)
        if raw is None:
            return None
        with self._op, self._apply:
            ptr = self.pointer.read()
            try:
                f.unlink()
            except OSError:
                pass
            if ptr is None or not ptr.trial or not ptr.previous:
                return None
            reason = str(raw.get("reason") if isinstance(raw, dict) else "") or "la versión a prueba falla al arrancar"
            j = self.journal.read()
            if j is not None and j.kind == "release" and j.to == ptr.active and j.in_progress:
                return self._rollback(j, f"vmshost: {reason}", blacklist=True)
            bad = ptr.active
            out = self.manual_rollback(ptr.previous, f"vmshost: {reason}")
            self.blacklist.add(bad, reason)
            return out

    def handle_directive_rollback(self) -> Outcome | None:
        d = read_directive(self.layout.directive_file)
        if d is None or not d.rollback_to:
            return None
        done_marker = self.layout.updater_data / "directive-done.json"
        prev = read_json(done_marker)
        key = f"{d.rollback_to}|{d.received}"
        if isinstance(prev, dict) and prev.get("rollback") == key:
            return None
        atomic_write_json(done_marker, {**(prev if isinstance(prev, dict) else {}), "rollback": key})
        to = None if d.rollback_to == "previous" else d.rollback_to
        return self.manual_rollback(to, "pedido desde el panel central")

    def handle_directive_unskip(self) -> list[str] | None:
        """«Permitir de nuevo» del panel central (directiva `unskip_at`), una sola vez por petición."""
        d = read_directive(self.layout.directive_file)
        if d is None or not d.unskip_at:
            return None
        done_marker = self.layout.updater_data / "directive-done.json"
        prev = read_json(done_marker)
        if isinstance(prev, dict) and prev.get("unskip") == d.unskip_at:
            return None
        atomic_write_json(done_marker, {**(prev if isinstance(prev, dict) else {}), "unskip": d.unskip_at})
        return self.unskip(None)

    # ================================================================== actualizador A/B
    def _slots_file(self) -> Path:
        return self.layout.updater_data / "slots.json"

    def _maybe_update_updater(self, client: TufClient, desc: ReleaseDescriptor) -> Outcome | None:
        ref = desc.components.get("updater")
        if ref is None:
            return None
        ptr = self.pointer.read()
        if ptr is None:
            return None
        slots = read_json(self._slots_file()) or {}
        cur = slots.get(ptr.updater.slot) if isinstance(slots, dict) else None
        if isinstance(cur, dict) and cur.get("sha256") == ref.sha256:
            return None
        if cur is None and ref.version == __version__:
            # Ranura puesta por el instalador sin slots.json: ya es este actualizador. Se anota y no se reinstala.
            slots = slots if isinstance(slots, dict) else {}
            slots[ptr.updater.slot] = {"version": ref.version, "sha256": ref.sha256}
            atomic_write_json(self._slots_file(), slots)
            return None
        key = f"updater-{ref.version}-{ref.sha256[:12]}"
        if self.blacklist.contains(key):
            return None
        new_slot = "b" if ptr.updater.slot == "a" else "a"
        j = Journal(update_id="s-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.d.clock())), kind="updater",
                    from_=ptr.updater.slot, to=new_slot, components=["updater"], services=["VMSUpdater"],
                    state="idle", attempt=1, last_good=ptr.active, reason=key)
        self.journal.write(j)
        j = self.journal.begin(j, "downloaded")

        def verify(slot_tmp: Path, files: list[str]) -> None:
            # La ranura lleva vmsctl.exe y el runtime, que corren como LocalSystem: misma segunda capa
            # (Authenticode) que el resto de binarios de la versión, además de TUF.
            pol = desc.authenticode
            if pol is None or self.d.verifier is None:
                return
            n = verify_tree(slot_tmp, files, self.d.verifier, subject_o=pol.subject_o, subject_c=pol.subject_c,
                            issuers=pol.issuers, third_party=pol.third_party)
            log.info("Authenticode: %d binarios del actualizador %s comprobados", n, ref.version)

        try:
            path = client.download(ref.target)
            install_updater_slot(slot_dir=self.layout.slot_dir(new_slot), zip_path=path, verify=verify)
        except (StageError, AuthenticodeError) as exc:
            msg = exc.message_es
            self.blacklist.add(key, msg)
            j = self.journal.begin(j.model_copy(update={"error": msg}), "rolled_back")
            self.journal.done(j)
            return self._finish("update_failed", f"El actualizador {ref.version} no supera la verificación: {msg}")
        except (UpdateSourceError, OSError) as exc:
            msg = getattr(exc, "message_es", str(exc))
            j = self.journal.begin(j.model_copy(update={"error": msg}), "rolled_back")
            self.journal.done(j)
            return self._finish("error", f"No se pudo preparar el actualizador nuevo: {msg}")
        j = self.journal.done(j)
        slots = slots if isinstance(slots, dict) else {}
        slots[new_slot] = {"version": ref.version, "sha256": ref.sha256}
        atomic_write_json(self._slots_file(), slots)
        j = self.journal.begin(j, "switched")
        self.pointer.set_updater_slot(new_slot, trial=True)
        j = self.journal.done(j)
        j = self.journal.begin(j, "started")
        self._set_status(state="started", message_es=f"Cambiando al actualizador nuevo ({ref.version})")
        return Outcome("restart_updater", f"Actualizador {ref.version} instalado en la ranura {new_slot.upper()}: "
                       "se reinicia el servicio", applied=ref.version)

    def _recover_updater_slot(self) -> Outcome | None:
        """Al arrancar en una ranura a prueba: NO se confirma todavía. La confirmación llega con la primera
        comprobación TUF correcta (`_confirm_updater_if_pending`); si este actualizador arranca pero no sabe
        comprobar, `vmshost` vuelve a la ranura anterior a los 30 min (CONTRATO §13.3)."""
        ptr = self.pointer.read()
        if ptr is None or not ptr.updater.trial:
            return None
        upd = ptr.updater
        mine = self.d.slot
        if mine is None:
            log.warning("Ranura %s del actualizador a prueba, pero no se sabe en qué ranura corre este proceso: "
                        "no se confirma", upd.slot.upper())
            return None
        if mine == upd.slot:
            log.info("Actualizador %s a prueba en la ranura %s: se confirmará tras la primera comprobación "
                     "correcta", __version__, upd.slot.upper())
            self._set_status(state="verifying", message_es=f"Actualizador {__version__} a prueba (ranura "
                             f"{upd.slot.upper()}): se confirma tras la primera comprobación correcta")
        return None

    def updater_trial_pending(self) -> bool:
        """¿Este proceso es el actualizador nuevo, todavía a prueba?"""
        ptr = self.pointer.read()
        return bool(ptr is not None and ptr.updater.trial and self.d.slot is not None
                    and self.d.slot == ptr.updater.slot)

    def _confirm_updater_if_pending(self) -> None:
        ptr = self.pointer.read()
        if ptr is None or not ptr.updater.trial or self.d.slot is None or self.d.slot != ptr.updater.slot:
            return
        with self._apply:
            self.pointer.confirm_updater()
            j = self.journal.read()
            if j is not None and j.kind == "updater" and j.in_progress:
                j = self.journal.done(j) if j.current_step and j.current_step.done_unix is None else j
                j = self.journal.begin(j, "good")
                self.journal.done(j)
        log.info("Ranura %s del actualizador confirmada", ptr.updater.slot.upper())
        self._set_status(last_result="update_ok", message_es=f"Actualizador {__version__} en marcha "
                         f"(ranura {ptr.updater.slot.upper()})", state="good")

    def _recover_updater_journal(self, j: Journal) -> Outcome | None:
        ptr = self.pointer.read()
        target_slot = j.to
        running_target = ptr is not None and ptr.updater.slot == target_slot
        if running_target and ptr is not None and not ptr.updater.trial:
            j = self.journal.begin(j, "good")
            self.journal.done(j)
            return Outcome("update_ok", "actualizador confirmado")
        if j.state == "downloaded" or (j.state == "switched" and not running_target):
            # no llegó a cambiar de ranura: se cierra el diario y se reintentará
            if ptr is not None and ptr.updater.slot == target_slot and ptr.updater.trial:
                self.pointer.set_updater_slot(j.from_ or "a", trial=False)
            j = self.journal.begin(j.model_copy(update={"error": "interrumpida"}), "rolled_back")
            self.journal.done(j)
            return self._finish("update_failed", "La actualización del actualizador se interrumpió; se reintentará")
        if j.state in ("started", "switched") and running_target and ptr is not None and ptr.updater.trial:
            if self.d.slot is None or self.d.slot == target_slot:
                # somos el actualizador nuevo (o no se sabe): se confirma tras la primera comprobación correcta
                return None
            # sigue a prueba y somos la ranura anterior (p. ej. reinicio antes de que arrancara la nueva):
            return Outcome("restart_updater", "el actualizador nuevo está pendiente de arrancar")
        # vmshost volvió a la ranura anterior: el nuevo no arrancó o no confirmó a tiempo
        if j.reason:
            self.blacklist.add(j.reason, "el actualizador nuevo no arrancó")
        j = self.journal.begin(j.model_copy(update={"error": "el actualizador nuevo no arrancó"}), "rolled_back")
        self.journal.done(j)
        return self._finish("update_failed", "El actualizador nuevo no arrancó: vmshost volvió a la ranura "
                            f"{(ptr.updater.slot if ptr else '?').upper()} y no se reintenta esa versión")


class _StepFailed(Exception):
    def __init__(self, message_es: str) -> None:
        super().__init__(message_es)
        self.message_es = message_es


class _DiskFull(Exception):
    def __init__(self, message_es: str) -> None:
        super().__init__(message_es)
        self.message_es = message_es


def current_slot() -> str | None:
    env = os.environ.get("VMS_UPDATER_SLOT", "").strip().lower()
    if env in ("a", "b"):
        return env
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.name in ("slot-a", "slot-b"):
            return parent.name[-1]
    return None


__all__ = ["Engine", "EngineDeps", "Outcome", "RELOAD_EXIT_CODE", "current_slot"]
