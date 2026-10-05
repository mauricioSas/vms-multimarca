"""Respondedor SADP (Hikvision) para pruebas: UDP unicast en 127.0.0.1, puerto libre.

Contesta al `inquiry` con un `ProbeMatch` por equipo configurado (con el Uuid de la sonda), como hace la
herramienta SADP. Nunca acepta otros mensajes (activación, reinicio de contraseña): los cuenta en
`other_messages` para que las pruebas comprueben que el programa no los manda.

    with SadpResponder([SadpTarget(host="192.168.1.64", serial="…", mac="…")]) as r:
        found = await vms.vendors.sadp.search(1.0, targets=[("127.0.0.1", r.port)], multicast=False)
"""
from __future__ import annotations

import re
import socket
import threading
from dataclasses import dataclass, field
from xml.sax.saxutils import escape


@dataclass
class SadpTarget:
    host: str
    model: str = "DS-2CD2143G2-I"
    serial: str = "DS-2CD2143G2-I20210315AAWRG12345678"
    mac: str = "c4-2f-90-f1-e6-a1"
    http_port: int = 80
    firmware: str = "V5.7.3build 220112"
    device_type: str = "138"
    activated: bool = True
    digital_channels: int = 1


def probe_match(uuid: str, t: SadpTarget) -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8"?><ProbeMatch>'
            f"<Uuid>{escape(uuid)}</Uuid><Types>inquiry</Types><DeviceType>{t.device_type}</DeviceType>"
            f"<DeviceDescription>{escape(t.model)}</DeviceDescription><DeviceSN>{escape(t.serial)}</DeviceSN>"
            f"<CommandPort>8000</CommandPort><HttpPort>{t.http_port}</HttpPort><MAC>{t.mac}</MAC>"
            f"<IPv4Address>{t.host}</IPv4Address><IPv4SubnetMask>255.255.255.0</IPv4SubnetMask>"
            "<IPv4Gateway>192.168.1.1</IPv4Gateway><IPv6Address>::</IPv6Address><IPv6Gateway>::</IPv6Gateway>"
            "<IPv6MaskLen>64</IPv6MaskLen><DHCP>false</DHCP><AnalogChannelNum>0</AnalogChannelNum>"
            f"<DigitalChannelNum>{t.digital_channels}</DigitalChannelNum>"
            f"<SoftwareVersion>{escape(t.firmware)}</SoftwareVersion><DSPVersion>V7.3 build 220112</DSPVersion>"
            f"<BootTime>2026-10-01 08:00:00</BootTime><Encrypt>true</Encrypt><ResetAbility>false</ResetAbility>"
            f"<Activated>{'true' if t.activated else 'false'}</Activated><PasswordResetAbility>false</PasswordResetAbility>"
            "</ProbeMatch>").encode("utf-8")


@dataclass
class SadpResponder:
    targets: list[SadpTarget]
    host: str = "127.0.0.1"
    port: int = 0
    probes_received: int = 0
    other_messages: int = 0
    _sock: socket.socket | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def start(self) -> SadpResponder:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((self.host, self.port))
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._run, name="sadp-responder", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._sock:
            self._sock.close()

    def __enter__(self) -> SadpResponder:
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
            if "<Types>inquiry</Types>" not in text:
                self.other_messages += 1
                continue
            self.probes_received += 1
            m = re.search(r"<Uuid>([^<]+)</Uuid>", text)
            uuid = m.group(1) if m else ""
            for t in self.targets:
                try:
                    self._sock.sendto(probe_match(uuid, t), addr)
                except OSError:
                    return
