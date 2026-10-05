"""Puntuación de marca a partir de pistas (WS-Discovery, SADP, DHIP, cabecera HTTP, modelo, MAC).

Pesos (PLAN-V2 §3.2 punto 4): lo que el equipo dice de sí mismo (fabricante ONVIF, `name/` y `hardware/` de
los scopes WSD, respuesta SADP o DHIP) pesa más que el prefijo del modelo, y el prefijo más que la MAC o la
cabecera `Server` de su web. «IPC-» ya no implica Dahua: también lo usan Uniview e Imou.

Las señales se combinan como probabilidades independientes (1 − Π(1 − pᵢ)), con tope 0,99, y una señal
de otra marca resta (un «name/HIKVISION» hunde a Dahua aunque el modelo empiece por «IPC-»).
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import unquote

from vms.core.interfaces import DetectionHints

W_SELF = 0.9          # fabricante o scope name/ con la marca
W_PROTOCOL = 0.95     # respondió al descubrimiento propio de la marca (SADP, DHIP)
W_MODEL = 0.75        # prefijo de modelo inequívoco
W_MODEL_WEAK = 0.35   # prefijo compartido con otras marcas
W_HTTP = 0.5          # cabecera Server de la web
W_MAC = 0.45          # OUI de la MAC (orientativo: hay OEM y MAC cambiadas)


def scope_values(scopes: Iterable[str], kind: str) -> list[str]:
    """Valores de los scopes `onvif://www.onvif.org/<kind>/<valor>` (decodificados, en mayúsculas)."""
    out: list[str] = []
    needle = f"/{kind.lower()}/"
    for s in scopes:
        low = s.lower()
        i = low.find(needle)
        if i >= 0:
            out.append(unquote(s[i + len(needle):]).upper())
    return out


def _norm_mac(mac: str) -> str:
    hexes = re.sub(r"[^0-9a-f]", "", mac.lower())
    return ":".join(hexes[i:i + 2] for i in range(0, min(len(hexes), 12), 2))


@dataclass(frozen=True)
class Detector:
    """Detector declarativo de una marca. Se usa como `DriverSpec.detect`."""

    names: tuple[str, ...]                                  # «HIKVISION», «HILOOK»… (en mayúsculas)
    model_regex: str | None = None                          # prefijo inequívoco
    weak_model_regex: str | None = None                     # prefijo compartido
    http_servers: tuple[str, ...] = ()                      # subcadenas de la cabecera Server
    ouis: tuple[str, ...] = ()                              # «c4:2f:90»…
    sadp: bool = False
    dhip: bool = False
    rivals: tuple[str, ...] = ()                            # nombres de otras marcas que restan

    def __call__(self, hints: DetectionHints) -> float:
        signals: list[float] = []
        self_texts = [hints.manufacturer.upper(), hints.name.upper(),
                      *scope_values(hints.scopes, "name"), *scope_values(hints.scopes, "hardware"),
                      *scope_values(hints.scopes, "manufacturer")]
        if any(n in t for t in self_texts for n in self.names if t):
            signals.append(W_SELF)
        if self.sadp and hints.sadp:
            signals.append(W_PROTOCOL)
        if self.dhip and hints.dhip:
            signals.append(W_PROTOCOL)
        models = [hints.model.upper().strip(), *scope_values(hints.scopes, "hardware")]
        if self.model_regex and any(re.match(self.model_regex, m, re.IGNORECASE) for m in models if m):
            signals.append(W_MODEL)
        elif self.weak_model_regex and any(re.match(self.weak_model_regex, m, re.IGNORECASE) for m in models if m):
            signals.append(W_MODEL_WEAK)
        server = hints.http_server.lower()
        if server and any(s.lower() in server for s in self.http_servers):
            signals.append(W_HTTP)
        mac = _norm_mac(hints.mac)
        if mac and any(mac.startswith(o) for o in self.ouis):
            signals.append(W_MAC)
        if not signals:
            return 0.0
        miss = 1.0
        for s in signals:
            miss *= 1.0 - s
        score = 1.0 - miss
        if any(r in t for t in self_texts for r in self.rivals if t) and W_SELF not in signals:
            score *= 0.3       # el equipo dice ser de otra marca
        return round(min(score, 0.99), 3)


def constant(value: float) -> Callable[[DetectionHints], float]:
    def detect(_hints: DetectionHints) -> float:
        return value
    return detect


__all__ = ["Detector", "constant", "scope_values"]
