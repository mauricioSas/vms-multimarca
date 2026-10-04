"""Detector (pre/post-proceso y modelo real) y lector de vídeo con patrón «último frame»."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import pytest

from analytics.detector import (
    COCO_PERSON_SLOT,
    DetectorRegistry,
    DetectorUnavailable,
    decode_persons,
    model_files,
    preprocess,
)
from analytics.video import Backoff, FrameGrabber, RateMeter

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "models"


# =========================================================================== pre/post-proceso
def test_preprocess_shape_and_normalization() -> None:
    img = np.full((360, 640, 3), 255, dtype=np.uint8)
    t = preprocess(img, 384)
    assert t.shape == (1, 3, 384, 384) and t.dtype == np.float32
    # blanco normalizado con la media/desviación de ImageNet (canal R)
    assert abs(float(t[0, 0, 0, 0]) - (1 - 0.485) / 0.229) < 1e-4


def test_decode_only_persons_above_threshold_in_pixels() -> None:
    q, c = 4, 91
    logits = np.full((q, c), -10.0, dtype=np.float32)
    logits[0, COCO_PERSON_SLOT] = 3.0     # persona segura (0,95)
    logits[1, COCO_PERSON_SLOT] = -1.0    # persona dudosa (0,27)
    logits[2, 3] = 5.0                    # coche: se ignora
    logits[3, COCO_PERSON_SLOT] = 2.0     # otra persona, misma caja que la 0 → NMS la quita
    boxes = np.array([[0.5, 0.5, 0.2, 0.4], [0.1, 0.1, 0.1, 0.1], [0.3, 0.3, 0.1, 0.1], [0.5, 0.5, 0.2, 0.4]],
                     dtype=np.float32)
    det = decode_persons(boxes, logits, (1000, 500), threshold=0.5)
    assert len(det) == 1
    np.testing.assert_allclose(det.xyxy[0], [400, 150, 600, 350], atol=1e-3)
    assert det.confidence[0] > 0.9 and det.class_id[0] == 0
    assert len(decode_persons(boxes, logits, (1000, 500), threshold=0.2)) == 2


def test_unknown_or_forbidden_model_rejected() -> None:
    with pytest.raises(DetectorUnavailable):
        model_files(MODELS, "rfdetr-xlarge")


def test_missing_model_files_give_clear_error(tmp_path: Path) -> None:
    reg = DetectorRegistry(tmp_path, "onnxruntime")
    with pytest.raises(DetectorUnavailable, match="export_model"):
        reg.get("rfdetr-nano")


# =========================================================================== modelo real
def _people_frame(people_video: Path) -> np.ndarray:
    import cv2

    cap = cv2.VideoCapture(str(people_video))
    cap.set(cv2.CAP_PROP_POS_FRAMES, 45)
    ok, img = cap.read()
    cap.release()
    assert ok
    return img


@pytest.mark.needs_model
@pytest.mark.parametrize("backend", ["openvino", "onnxruntime"])
def test_real_model_detects_people(backend: str, people_video: Path) -> None:
    if not (MODELS / "rfdetr-nano.onnx").is_file():
        pytest.skip("Falta models/rfdetr-nano.onnx: python -m analytics.tools.export_model")
    det = DetectorRegistry(MODELS, backend).get("rfdetr-nano")
    assert det.backend == backend
    img = _people_frame(people_video)
    found = det.detect(img, 0.4)
    assert len(found) >= 5, f"solo {len(found)} personas"
    assert (found.xyxy[:, 2] <= img.shape[1]).all() and (found.xyxy[:, 3] <= img.shape[0]).all()


@pytest.mark.needs_model
def test_openvino_and_onnxruntime_agree(people_video: Path) -> None:
    if not (MODELS / "rfdetr-nano.onnx").is_file():
        pytest.skip("Falta models/rfdetr-nano.onnx")
    img = _people_frame(people_video)
    a = DetectorRegistry(MODELS, "openvino").get("rfdetr-nano").detect(img, 0.4)
    b = DetectorRegistry(MODELS, "onnxruntime").get("rfdetr-nano").detect(img, 0.4)
    assert abs(len(a) - len(b)) <= 1   # f32 en ambos: mismas personas salvo empates en el umbral


# =========================================================================== lector de vídeo
class FakeCapture:
    """Simula cv2.VideoCapture: entrega `n` frames numerados y luego «se corta»."""

    def __init__(self, n: int, delay: float = 0.005, opened: bool = True) -> None:
        self.n, self.delay, self.opened, self.i = n, delay, opened, 0
        self.released = False

    def isOpened(self) -> bool:  # noqa: N802 (API de OpenCV)
        return self.opened

    def read(self) -> tuple[bool, np.ndarray | None]:
        if self.i >= self.n:
            return False, None
        time.sleep(self.delay)
        self.i += 1
        img = np.zeros((4, 6, 3), dtype=np.uint8)
        img[0, 0, 0] = self.i % 256
        return True, img

    def release(self) -> None:
        self.released = True


def test_grabber_keeps_only_latest_frame() -> None:
    caps = [FakeCapture(200, delay=0.001)]
    g = FrameGrabber("rtsp://127.0.0.1:1/x", "prueba", capture_factory=lambda url: caps[0] if caps else None,
                     backoff=(0.05,))
    g.start()
    first = g.wait_newer(0, timeout=2)
    assert first is not None
    time.sleep(0.1)                         # el consumidor «tarda»: llegan muchos frames
    latest = g.latest()
    assert latest is not None and latest.seq > first.seq + 5   # se saltó los intermedios
    assert g.frame_size == (6, 4)
    g.stop()


def test_grabber_reconnects_with_backoff() -> None:
    attempts: list[float] = []
    lock = threading.Lock()

    def factory(url: str) -> FakeCapture:
        with lock:
            attempts.append(time.monotonic())
            n = len(attempts)
        if n == 1:
            return FakeCapture(0, opened=False)   # primer intento: no abre
        if n == 2:
            return FakeCapture(3)                 # segundo: 3 frames y se corta
        return FakeCapture(10_000)

    g = FrameGrabber("rtsp://usuario:clave@127.0.0.1:1/x", capture_factory=factory, backoff=(0.05, 0.1))
    g.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (len(attempts) < 3 or g.state != "running"):
        time.sleep(0.02)
    assert len(attempts) >= 3 and g.state == "running" and g.reconnects >= 2
    assert attempts[1] - attempts[0] >= 0.04 and attempts[2] - attempts[1] >= 0.09
    assert "clave" not in g.name   # el nombre por defecto oculta credenciales
    g.stop()
    assert g.state == "stopped"


def test_backoff_and_rate_meter() -> None:
    b = Backoff()
    assert [b.next() for _ in range(7)] == [1, 2, 5, 10, 30, 30, 30]
    b.reset()
    assert b.next() == 1
    rm = RateMeter(window_s=5)
    for i in range(11):
        rm.tick(100 + i * 0.1)
    assert abs(rm.rate(101.0) - 10.0) < 0.01
    assert rm.rate(110.0) == 0.0     # flujo parado
