"""Persistencia de config.json y users.json con guardado atómico, copia y recuperación.

- Antes de sustituir config.json, la versión anterior válida se copia a config.json.bak.
- Si config.json está dañado se aparta como config.corrupt-FECHA.json (nunca se sobrescribe),
  se intenta la copia .bak y se devuelve un aviso para el usuario.
- Si al cargar hubo que corregir datos (referencias rotas, duplicados), el original se conserva
  como config.original-FECHA.json antes de que un guardado lo reescriba.

ConfigRepository es el único punto de escritura en el proceso del backend: serializa los
cambios con un asyncio.Lock y avisa a los suscriptores (motor, SSE) tras cada guardado.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from pydantic import ValidationError

from .atomic import atomic_write_text
from .models import AppConfig, User
from .paths import restrict_permissions

log = logging.getLogger(__name__)

T = TypeVar("T")
Listener = Callable[[AppConfig], Awaitable[None] | None]


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.backup_path = self.path.with_name(self.path.name + ".bak")

    def _read(self, path: Path) -> tuple[AppConfig, list[str]]:
        data = json.loads(path.read_text(encoding="utf-8"))
        try:
            cfg = AppConfig.model_validate(data)
        except ValidationError as exc:
            raise ValueError(f"estructura no válida: {exc.error_count()} errores") from exc
        problems = cfg.repair()
        if isinstance(data, dict):
            for d in data.get("devices", []) or []:
                if isinstance(d, dict) and "password" in d:
                    problems.append(f"Se ignoró una contraseña en texto plano del equipo «{d.get('name', '?')}»; "
                                    "vuelve a escribirla.")
        return cfg, problems

    def _stamp_name(self, kind: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        stem = self.path.stem
        target = self.path.with_name(f"{stem}.{kind}-{stamp}.json")
        n = 1
        while target.exists():
            target = self.path.with_name(f"{stem}.{kind}-{stamp}-{n}.json")
            n += 1
        return target

    def load(self) -> tuple[AppConfig, str | None]:
        """Devuelve (configuración, aviso_para_el_usuario_o_None)."""
        if not self.path.exists():
            if self.backup_path.exists():
                try:
                    cfg, _ = self._read(self.backup_path)
                    return cfg, "No se encontró config.json; se cargó la copia de seguridad."
                except (OSError, ValueError) as exc:
                    log.warning("Copia de seguridad no válida: %s", exc)
            return AppConfig(), None
        try:
            cfg, problems = self._read(self.path)
        except (OSError, ValueError) as exc:  # JSONDecodeError es ValueError
            log.error("config.json dañado: %s", exc)
            moved = self._stamp_name("corrupt")
            os.replace(self.path, moved)
            msg = (f"El archivo de configuración estaba dañado ({exc}). "
                   f"Se conservó como «{moved.name}». ")
            if self.backup_path.exists():
                try:
                    cfg, _ = self._read(self.backup_path)
                    when = datetime.fromtimestamp(self.backup_path.stat().st_mtime).strftime("%d/%m/%Y %H:%M")
                    return cfg, msg + f"Se cargó la copia de seguridad del {when}. Revisa que esté todo."
                except (OSError, ValueError) as exc2:
                    log.error("La copia de seguridad también está dañada: %s", exc2)
            return AppConfig(), msg + "No había copia válida: se empieza con una configuración vacía."
        if problems:
            keep = self._stamp_name("original")
            shutil.copy2(self.path, keep)
            return cfg, ("Se corrigieron problemas en la configuración: " + "; ".join(problems)
                         + f". El original se conservó como «{keep.name}».")
        return cfg, None

    def save(self, cfg: AppConfig) -> None:
        text = cfg.model_dump_json(indent=2)
        if self.path.exists():
            try:
                current = self.path.read_text(encoding="utf-8")
                json.loads(current)
                atomic_write_text(self.backup_path, current)
            except (OSError, ValueError):
                log.warning("No se actualizó la copia de seguridad: el config.json actual no es válido")
        atomic_write_text(self.path, text)


class ConfigRepository:
    """Acceso concurrente seguro a la configuración dentro del backend."""

    def __init__(self, store: ConfigStore) -> None:
        self.store = store
        self._lock = asyncio.Lock()
        self._listeners: list[Listener] = []
        self.config, self.load_warning = store.load()
        if self.load_warning:
            log.warning("%s", self.load_warning)
        self.revision = 0

    def snapshot(self) -> AppConfig:
        """Copia profunda de solo lectura (modificarla no afecta a la guardada)."""
        return self.config.model_copy(deep=True)

    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    async def update(self, mutate: Callable[[AppConfig], T]) -> T:
        """Aplica `mutate` sobre una copia; si no lanza, guarda y notifica. Devuelve su resultado."""
        async with self._lock:
            draft = self.config.model_copy(deep=True)
            result = mutate(draft)
            draft = AppConfig.model_validate(draft.model_dump())  # revalida todo el documento
            await asyncio.to_thread(self.store.save, draft)
            self.config = draft
            self.revision += 1
        await self._notify(draft)
        return result

    async def _notify(self, cfg: AppConfig) -> None:
        for listener in list(self._listeners):
            try:
                res = listener(cfg.model_copy(deep=True))
                if inspect.isawaitable(res):
                    await res
            except Exception:  # un suscriptor roto no debe impedir guardar ni a los demás
                log.exception("Error en un suscriptor de cambios de configuración")


class UserStore:
    """users.json: lista de usuarios con hash argon2 de la contraseña."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self.users: dict[str, User] = self._load()

    def _load(self) -> dict[str, User]:
        if not self.path.exists():
            return {}
        try:
            raw: Any = json.loads(self.path.read_text(encoding="utf-8"))
            users = [User.model_validate(u) for u in raw.get("users", [])]
        except (OSError, ValueError, AttributeError) as exc:
            log.error("users.json dañado (%s); se aparta y se empieza sin usuarios", exc)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            os.replace(self.path, self.path.with_name(f"users.corrupt-{stamp}.json"))
            return {}
        return {u.username.lower(): u for u in users}

    def get(self, username: str) -> User | None:
        return self.users.get(username.lower())

    def all(self) -> list[User]:
        return sorted(self.users.values(), key=lambda u: u.username.lower())

    async def save_user(self, user: User) -> None:
        async with self._lock:
            self.users[user.username.lower()] = user
            await asyncio.to_thread(self._write)

    async def delete_user(self, username: str) -> None:
        async with self._lock:
            self.users.pop(username.lower(), None)
            await asyncio.to_thread(self._write)

    def _write(self) -> None:
        data = {"users": [json.loads(u.model_dump_json()) for u in self.all()]}
        atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=2))
        restrict_permissions(self.path)
