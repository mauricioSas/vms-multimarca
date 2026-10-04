"""Mide la velocidad del detector en ESTE equipo (para dimensionar el mini PC de tienda).

    python -m analytics.tools.benchmark --video tests/assets/people-walking-h264.mp4 \\
        --model rfdetr-nano --model rfdetr-small --backend openvino --backend onnxruntime

Para cada combinación modelo/backend procesa N imágenes del vídeo (en memoria) y muestra:
- ms de inferencia por imagen (mediana p50 y p95), incluido el preprocesado;
- imágenes por segundo que puede analizar UNA cámara (1000 / p50);
- personas detectadas por imagen (media), para comparar la calidad entre tamaños.

Regla práctica: la suma de los fps configurados de todas las cámaras de la tienda debe quedar
por debajo de lo que mide esta herramienta con todos los núcleos, con margen (≈ 70 %).
"""
from __future__ import annotations

import argparse
import json
import logging
import platform
import sys
import time
from pathlib import Path

import numpy as np

import analytics  # noqa: F401  (bloqueos de licencias/telemetría)
from analytics.detector import MODEL_SPECS, DetectorRegistry, DetectorUnavailable

DEFAULT_MODELS = Path(__file__).resolve().parents[2] / "models"


def load_frames(video: Path, n: int) -> list[np.ndarray]:
    import cv2

    cap = cv2.VideoCapture(str(video))
    frames: list[np.ndarray] = []
    try:
        while len(frames) < n:
            ok, img = cap.read()
            if not ok:
                if not frames:
                    raise SystemExit(f"No se pudo leer el vídeo {video}")
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                continue
            frames.append(img)
    finally:
        cap.release()
    return frames


def bench(models_dir: Path, model: str, backend: str, frames: list[np.ndarray], threshold: float,
          warmup: int = 3) -> dict[str, object]:
    det = DetectorRegistry(models_dir, backend).get(model)
    for img in frames[:warmup]:
        det.detect(img, threshold)
    times: list[float] = []
    persons: list[int] = []
    for img in frames:
        t0 = time.perf_counter()
        d = det.detect(img, threshold)
        times.append((time.perf_counter() - t0) * 1000)
        persons.append(len(d))
    p50 = float(np.percentile(times, 50))
    return {"model": model, "backend": det.backend, "frames": len(frames),
            "input": f"{frames[0].shape[1]}x{frames[0].shape[0]}",
            "ms_p50": round(p50, 1), "ms_p95": round(float(np.percentile(times, 95)), 1),
            "fps_max_one_camera": round(1000.0 / p50, 1), "persons_mean": round(float(np.mean(persons)), 1)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Velocidad del detector en este equipo")
    p.add_argument("--video", type=Path, required=True)
    p.add_argument("--model", action="append", choices=sorted(MODEL_SPECS))
    p.add_argument("--backend", action="append", choices=["openvino", "onnxruntime", "torch"])
    p.add_argument("--frames", type=int, default=60)
    p.add_argument("--threshold", type=float, default=0.4)
    p.add_argument("--models-dir", type=Path, default=DEFAULT_MODELS)
    p.add_argument("--json", action="store_true", help="salida en JSON")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    frames = load_frames(args.video, args.frames)
    results = []
    for model in args.model or ["rfdetr-nano"]:
        for backend in args.backend or ["openvino", "onnxruntime"]:
            try:
                results.append(bench(args.models_dir, model, backend, frames, args.threshold))
            except DetectorUnavailable as exc:
                results.append({"model": model, "backend": backend, "error": str(exc)})
    cpu = platform.processor() or platform.machine()
    if args.json:
        print(json.dumps({"cpu": cpu, "python": sys.version.split()[0], "results": results}, indent=1))
        return 0
    print(f"Equipo: {platform.system()} {platform.machine()} ({cpu}) · {len(frames)} imágenes de {args.video.name}")
    print(f"{'modelo':<14}{'backend':<13}{'p50 ms':>8}{'p95 ms':>8}{'fps máx':>9}{'personas':>10}")
    for r in results:
        if "error" in r:
            print(f"{r['model']:<14}{r['backend']:<13}  ERROR: {r['error']}")
        else:
            print(f"{r['model']:<14}{r['backend']:<13}{r['ms_p50']:>8}{r['ms_p95']:>8}"
                  f"{r['fps_max_one_camera']:>9}{r['persons_mean']:>10}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
