"""Respondedor DHIP (Dahua, UDP 37810) para pruebas: UDP unicast en 127.0.0.1, puerto libre.

Solo responde a una sonda con cabecera DHIP de 32 bytes y el JSON `DHDiscover.search` (el JSON a pelo no
recibe respuesta, como en los equipos). Registra la hora de cada sonda para comprobar el límite de
5 paquetes por segundo del programa.
"""
from __future__ import annotations

import json
import socket
import struct
import threading
import time
from dataclasses import dataclass, field


@dataclass
class DhipTarget:
    host: str
    model: str = "DHI-NVR4208-8P-4KS2/L"
    serial: str = "6J0123PAZ12345"
    mac: str = "3c:ef:8c:12:34:56"
    http_port: int = 80
    version: str = "4.001.0000000.1"
    device_class: str = "NVR"
    vendor: str = "Dahua"
    channels: int = 8


def reply(t: DhipTarget) -> bytes:
    body = json.dumps({"method": "client.notifyDevInfo", "params": {"deviceInfo": {
        "SerialNo": t.serial, "DeviceType": t.model, "DeviceClass": t.device_class, "Vendor": t.vendor,
        "Version": t.version, "HttpPort": t.http_port, "Port": 37777, "Mac": t.mac, "MachineName": t.serial,
        "VideoInputChannels": 0, "RemoteVideoInputChannels": t.channels,
        "IPv4Address": {"IPAddress": t.host, "SubnetMask": "255.255.255.0", "DefaultGateway": "192.168.1.1"},
    }}}).encode("utf-8")
    header = struct.pack("<I", 32) + b"DHIP" + bytes(8) + struct.pack("<QQ", len(body), len(body))
    return header + body


@dataclass
class DhipResponder:
    targets: list[DhipTarget]
    host: str = "127.0.0.1"
    port: int = 0
    probe_times: list[float] = field(default_factory=list)
    bare_json: int = 0
    _sock: socket.socket | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def start(self) -> DhipResponder:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((self.host, self.port))
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._run, name="dhip-responder", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._sock:
            self._sock.close()

    def __enter__(self) -> DhipResponder:
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
            if data[4:8] != b"DHIP" or b"DHDiscover.search" not in data:
                self.bare_json += 1
                continue
            self.probe_times.append(time.monotonic())
            for t in self.targets:
                try:
                    self._sock.sendto(reply(t), addr)
                except OSError:
                    return
