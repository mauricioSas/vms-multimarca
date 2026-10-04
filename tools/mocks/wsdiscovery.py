"""Respondedor WS-Discovery para pruebas (UDP unicast en 127.0.0.1, puerto libre).

El descubrimiento real envía un Probe por multicast a 239.255.255.250:3702. En pruebas, la
función de descubrimiento de vms.vendors debe aceptar un destino alternativo (p. ej. el
parámetro `targets=[("127.0.0.1", responder.port)]`) para no depender de la red local.

    with WsDiscoveryResponder([ProbeTarget(...)]) as r:
        devices = await discover(timeout=1.0, targets=[("127.0.0.1", r.port)])
"""
from __future__ import annotations

import re
import socket
import threading
from dataclasses import dataclass, field

from .onvif import probe_match_xml


@dataclass
class ProbeTarget:
    address_uuid: str
    xaddr: str
    scopes: list[str]
    types: str = "dn:NetworkVideoTransmitter tds:Device"


@dataclass
class WsDiscoveryResponder:
    targets: list[ProbeTarget]
    host: str = "127.0.0.1"
    port: int = 0
    probes_received: int = 0
    _sock: socket.socket | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def start(self) -> "WsDiscoveryResponder":
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((self.host, self.port))
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._run, name="wsd-responder", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._sock:
            self._sock.close()

    def __enter__(self) -> "WsDiscoveryResponder":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _run(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(65535)
            except TimeoutError:
                continue
            except OSError:
                return
            text = data.decode("utf-8", errors="replace")
            if "Probe" not in text:
                continue
            self.probes_received += 1
            m = re.search(r"<(?:[\w.\-]+:)?MessageID[^>]*>([^<]+)</", text)
            relates = m.group(1).strip() if m else "uuid:unknown"
            for t in self.targets:
                reply = probe_match_xml(relates, address_uuid=t.address_uuid, xaddr=t.xaddr,
                                        scopes=t.scopes, types=t.types)
                try:
                    self._sock.sendto(reply.encode("utf-8"), addr)
                except OSError:
                    return
