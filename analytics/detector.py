"""Detección de personas con RF-DETR (Apache-2.0).

Qué hace un detector, en sencillo: recibe una imagen y devuelve rectángulos («cajas») donde
cree que hay una persona, cada uno con una «confianza» entre 0 y 1. No sabe QUIÉN es la
persona (no hay reconocimiento facial ni identificación): solo «aquí hay una persona».

Modelo: RF-DETR de Roboflow, tamaños nano/small/medium/base (licencia Apache-2.0). Los tamaños
XL/2XL tienen otra licencia (PML) y están prohibidos en este producto.

Cómo se ejecuta (backend intercambiable):
- **OpenVINO** (Intel): el más rápido en los mini PC Intel N150/i5 de tienda. También funciona
  en CPU ARM (Mac M1 de desarrollo).
- **ONNX Runtime**: alternativa portable (cualquier CPU).
- **PyTorch** (paquete `rfdetr`): solo en la máquina de desarrollo; en tienda no se instala
  torch (pesa varios GB).

Los dos primeros usan el modelo ya exportado a ONNX (`models/rfdetr-<tamaño>.onnx`, y el formato
propio de OpenVINO `.xml/.bin` si existe), que se genera con `python -m analytics.tools.export_model`.

Pasos de cada imagen:
1. Preprocesado: se escala a la resolución del modelo (p. ej. 384x384 en nano), se pasa de BGR a
   RGB y se normaliza con la media/desviación de ImageNet (lo mismo que hace RF-DETR por dentro).
2. Inferencia: el modelo devuelve 300 «propuestas», cada una con una caja y una puntuación por
   clase. RF-DETR no necesita el paso clásico de «supresión de no máximos» (NMS), pero aplicamos
   uno suave por seguridad para no contar dos cajas casi idénticas como dos personas.
3. Solo nos quedamos con la clase persona (COCO «person») por encima del umbral.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np
import supervision as sv

log = logging.getLogger("analytics.detector")

BackendName = Literal["openvino", "onnxruntime", "torch"]

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
# Los modelos preentrenados en COCO de RF-DETR usan los identificadores originales de COCO
# (1..90 con huecos); «person» es el 1. Si se exporta un modelo propio, su .json lo indica.
COCO_PERSON_SLOT = 1
NMS_IOU = 0.7


@dataclass(frozen=True)
class ModelSpec:
    name: str
    rfdetr_class: str
    resolution: int


# Solo tamaños con licencia Apache-2.0.
MODEL_SPECS: dict[str, ModelSpec] = {
    "rfdetr-nano": ModelSpec("rfdetr-nano", "RFDETRNano", 384),
    "rfdetr-small": ModelSpec("rfdetr-small", "RFDETRSmall", 512),
    "rfdetr-medium": ModelSpec("rfdetr-medium", "RFDETRMedium", 576),
    "rfdetr-base": ModelSpec("rfdetr-base", "RFDETRBase", 560),
}


class DetectorUnavailable(RuntimeError):
    """No se puede cargar el detector (falta el modelo exportado o el backend). Mensaje en español."""


class PersonDetector(Protocol):
    model: str
    backend: str

    def detect(self, image_bgr: np.ndarray, threshold: float) -> sv.Detections:
        """Personas con confianza >= threshold. xyxy en píxeles de la imagen, class_id = 0."""
        ...


class LatencyStats:
    """Mediana (p50) y p95 de los últimos N tiempos de inferencia, en milisegundos."""

    def __init__(self, size: int = 200) -> None:
        self._values: deque[float] = deque(maxlen=size)
        self._lock = threading.Lock()

    def add(self, ms: float) -> None:
        with self._lock:
            self._values.append(ms)

    def percentile(self, q: float) -> float | None:
        with self._lock:
            if not self._values:
                return None
            return float(np.percentile(np.fromiter(self._values, dtype=np.float64), q))


# =========================================================================== pre/post-proceso
def preprocess(image_bgr: np.ndarray, resolution: int) -> np.ndarray:
    """Imagen BGR (alto, ancho, 3) → tensor float32 (1, 3, R, R) normalizado como en RF-DETR.

    Se escala con interpolación bilineal sin antialias (cv2.INTER_LINEAR), que es la misma
    convención que usa RF-DETR al predecir; así los resultados coinciden con el modelo original.
    """
    import cv2

    resized = cv2.resize(image_bgr, (resolution, resolution), interpolation=cv2.INTER_LINEAR)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32)
    rgb *= 1.0 / 255.0
    rgb -= IMAGENET_MEAN
    rgb /= IMAGENET_STD
    return np.ascontiguousarray(rgb.transpose(2, 0, 1)[np.newaxis])


def decode_persons(boxes_cxcywh: np.ndarray, logits: np.ndarray, image_size: tuple[int, int],
                   threshold: float, person_slot: int = COCO_PERSON_SLOT) -> sv.Detections:
    """Salidas crudas de RF-DETR (una imagen) → personas en píxeles.

    boxes_cxcywh: (Q, 4) normalizadas 0..1 (centro x, centro y, ancho, alto).
    logits: (Q, C) puntuaciones sin normalizar; RF-DETR usa una sigmoide independiente por clase.
    image_size: (ancho, alto) de la imagen original.
    """
    scores = 1.0 / (1.0 + np.exp(-np.clip(logits[:, person_slot], -88.0, 88.0)))
    keep = scores >= threshold
    if not np.any(keep):
        return _empty()
    cx, cy, bw, bh = boxes_cxcywh[keep].T
    w, h = image_size
    scale = np.array([w, h, w, h], dtype=np.float32)
    xyxy = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1) * scale
    xyxy = np.clip(xyxy, 0.0, scale).astype(np.float32)
    conf = scores[keep].astype(np.float32)
    # Se descartan cajas degeneradas (ancho o alto < 2 px).
    valid = ((xyxy[:, 2] - xyxy[:, 0]) >= 2) & ((xyxy[:, 3] - xyxy[:, 1]) >= 2)
    det = sv.Detections(xyxy=xyxy[valid], confidence=conf[valid],
                        class_id=np.zeros(int(valid.sum()), dtype=int))
    if len(det) > 1:
        det = det.with_nms(threshold=NMS_IOU, class_agnostic=True)
    return det


def _empty() -> sv.Detections:
    return sv.Detections(xyxy=np.empty((0, 4), dtype=np.float32), confidence=np.empty(0, dtype=np.float32),
                         class_id=np.empty(0, dtype=int))


# =========================================================================== metadatos del modelo
@dataclass
class ModelFiles:
    model: str
    onnx: Path
    ir_xml: Path
    meta: Path
    resolution: int
    person_slot: int = COCO_PERSON_SLOT
    extra: dict[str, Any] = field(default_factory=dict)


def model_files(models_dir: Path, model: str) -> ModelFiles:
    if model not in MODEL_SPECS:
        raise DetectorUnavailable(f"Modelo desconocido o no permitido: {model}")
    spec = MODEL_SPECS[model]
    base = Path(models_dir)
    meta_path = base / f"{model}.json"
    meta: dict[str, Any] = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("No se pudo leer %s: %s (se usan valores por defecto)", meta_path.name, exc)
    return ModelFiles(model=model, onnx=base / f"{model}.onnx", ir_xml=base / f"{model}.xml", meta=meta_path,
                      resolution=int(meta.get("resolution", spec.resolution)),
                      person_slot=int(meta.get("person_class_index", COCO_PERSON_SLOT)), extra=meta)


# =========================================================================== backends
class _ExportedDetector:
    """Base común de ONNX Runtime y OpenVINO: mismo pre/post-proceso, distinto motor."""

    backend: str = ""

    def __init__(self, files: ModelFiles) -> None:
        self.model = files.model
        self.resolution = files.resolution
        self.person_slot = files.person_slot

    def _infer(self, tensor: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        raise NotImplementedError

    def detect(self, image_bgr: np.ndarray, threshold: float) -> sv.Detections:
        h, w = image_bgr.shape[:2]
        boxes, logits = self._infer(preprocess(image_bgr, self.resolution))
        return decode_persons(boxes[0], logits[0], (w, h), threshold, self.person_slot)


class OnnxRuntimeDetector(_ExportedDetector):
    backend = "onnxruntime"

    def __init__(self, files: ModelFiles, threads: int = 0) -> None:
        super().__init__(files)
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise DetectorUnavailable("ONNX Runtime no está instalado (pip install onnxruntime)") from exc
        if not files.onnx.is_file():
            raise DetectorUnavailable(
                f"Falta el modelo {files.onnx.name}: ejecútalo con «python -m analytics.tools.export_model»")
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        if threads:
            opts.intra_op_num_threads = threads
        opts.log_severity_level = 3
        # Una InferenceSession de ONNX Runtime admite run() desde varios hilos a la vez.
        self._session = ort.InferenceSession(str(files.onnx), opts, providers=["CPUExecutionProvider"])
        self._input = self._session.get_inputs()[0].name
        shape = self._session.get_inputs()[0].shape
        if isinstance(shape[-1], int):
            self.resolution = int(shape[-1])
        names = [o.name for o in self._session.get_outputs()]
        self._outputs = ["dets", "labels"] if {"dets", "labels"} <= set(names) else names[:2]

    def _infer(self, tensor: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        boxes, logits = self._session.run(self._outputs, {self._input: tensor})
        return boxes, logits


class OpenVinoDetector(_ExportedDetector):
    backend = "openvino"

    def __init__(self, files: ModelFiles, threads: int = 0, precision: str = "f32") -> None:
        super().__init__(files)
        try:
            import openvino as ov
        except ImportError as exc:
            raise DetectorUnavailable("OpenVINO no está instalado (pip install openvino)") from exc
        path = files.ir_xml if files.ir_xml.is_file() else files.onnx
        if not path.is_file():
            raise DetectorUnavailable(
                f"Falta el modelo {files.onnx.name}: ejecútalo con «python -m analytics.tools.export_model»")
        core = ov.Core()
        cfg: dict[str, Any] = {"PERFORMANCE_HINT": "LATENCY", "INFERENCE_PRECISION_HINT": precision}
        if threads:
            cfg["INFERENCE_NUM_THREADS"] = threads
        model = core.read_model(str(path))
        self._compiled = core.compile_model(model, "CPU", cfg)
        shape = self._compiled.input(0).get_partial_shape()
        if shape.is_static:
            self.resolution = int(shape[3].get_length())
        names = {o.get_any_name(): o for o in self._compiled.outputs}
        self._out_boxes = names.get("dets", self._compiled.output(0))
        self._out_logits = names.get("labels", self._compiled.output(1))
        # Una «petición de inferencia» por hilo: así varias cámaras pueden usar el mismo modelo.
        self._local = threading.local()
        log.info("OpenVINO: modelo %s cargado desde %s (precisión %s)", self.model, path.name, precision)

    def _infer(self, tensor: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        req = getattr(self._local, "req", None)
        if req is None:
            req = self._compiled.create_infer_request()
            self._local.req = req
        res = req.infer({0: tensor})
        return np.asarray(res[self._out_boxes]), np.asarray(res[self._out_logits])


class TorchDetector:
    """RF-DETR original en PyTorch (solo desarrollo; necesita el extra «export»)."""

    backend = "torch"

    def __init__(self, files: ModelFiles, weights_dir: Path, device: str = "cpu") -> None:
        import os

        os.environ.setdefault("RF_HOME", str(weights_dir))  # pesos dentro del proyecto, no en ~/.roboflow
        try:
            import rfdetr
        except ImportError as exc:
            raise DetectorUnavailable("El paquete rfdetr (PyTorch) no está instalado") from exc
        spec = MODEL_SPECS[files.model]
        self.model = files.model
        # device="cpu" por defecto para medir lo mismo que en tienda; "mps"/"cuda" solo en desarrollo.
        self._net = getattr(rfdetr, spec.rfdetr_class)(device=device)
        self.backend = f"torch-{device}"
        self._lock = threading.Lock()
        self.resolution = spec.resolution

    def detect(self, image_bgr: np.ndarray, threshold: float) -> sv.Detections:
        rgb = image_bgr[:, :, ::-1].copy()
        with self._lock:
            det = self._net.predict(rgb, threshold=threshold, include_source_image=False)
        assert isinstance(det, sv.Detections)
        keep = det.class_id == COCO_PERSON_SLOT if det.class_id is not None else np.zeros(len(det), bool)
        det = det[keep]
        if len(det) == 0:
            return _empty()
        return sv.Detections(xyxy=det.xyxy.astype(np.float32), confidence=det.confidence.astype(np.float32),
                             class_id=np.zeros(len(det), dtype=int))


# =========================================================================== fábrica con caché
def available_backends() -> list[BackendName]:
    import importlib.util

    found: list[BackendName] = []
    for mod, name in (("openvino", "openvino"), ("onnxruntime", "onnxruntime"), ("rfdetr", "torch")):
        try:
            if importlib.util.find_spec(mod) is not None:
                found.append(name)  # type: ignore[arg-type]
        except (ImportError, ValueError):
            continue
    return found


class DetectorRegistry:
    """Carga cada modelo una sola vez y lo comparte entre cámaras (ahorra memoria).

    Con `backend="auto"` se prueba en orden: OpenVINO → ONNX Runtime → PyTorch, y se usa el
    primero que cargue. Si ninguno funciona se lanza DetectorUnavailable con la explicación.
    """

    def __init__(self, models_dir: Path, backend: str = "auto", *, threads: int = 0,
                 openvino_precision: str = "f32") -> None:
        self.models_dir = Path(models_dir)
        self.backend = backend
        self.threads = threads
        self.openvino_precision = openvino_precision
        self._cache: dict[str, PersonDetector] = {}
        self._lock = threading.Lock()

    def get(self, model: str) -> PersonDetector:
        with self._lock:
            if model not in self._cache:
                self._cache[model] = self._load(model)
            return self._cache[model]

    def _load(self, model: str) -> PersonDetector:
        files = model_files(self.models_dir, model)
        order: list[str] = (["openvino", "onnxruntime", "torch"] if self.backend == "auto" else [self.backend])
        errors: list[str] = []
        for name in order:
            t0 = time.perf_counter()
            try:
                det: PersonDetector
                if name == "openvino":
                    det = OpenVinoDetector(files, self.threads, self.openvino_precision)
                elif name == "onnxruntime":
                    det = OnnxRuntimeDetector(files, self.threads)
                elif name == "torch":
                    det = TorchDetector(files, self.models_dir / "weights")
                else:
                    raise DetectorUnavailable(f"Backend desconocido: {name}")
            except DetectorUnavailable as exc:
                errors.append(f"{name}: {exc}")
                continue
            except Exception as exc:  # error interno del motor al cargar: se prueba el siguiente
                log.exception("Fallo al cargar %s con %s", model, name)
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            log.info("Detector %s listo con %s en %.1f s", model, name, time.perf_counter() - t0)
            return det
        raise DetectorUnavailable(f"No se pudo cargar el detector {model}. " + " | ".join(errors))
