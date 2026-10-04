"""Exporta RF-DETR (PyTorch) a ONNX y al formato IR de OpenVINO.

Se ejecuta UNA vez en la máquina de desarrollo o de preparación (necesita el extra `export`:
rfdetr + torch + onnx). Los archivos resultantes se copian a la carpeta `models/` de cada tienda,
que solo necesita OpenVINO u ONNX Runtime para ejecutarlos (sin torch).

    python -m analytics.tools.export_model --model rfdetr-nano --model rfdetr-small
    python -m analytics.tools.export_model --model rfdetr-nano --out models --formats onnx openvino

Resultado por modelo, en --out:
    rfdetr-nano.onnx      modelo ONNX (ONNX Runtime u OpenVINO)
    rfdetr-nano.xml/.bin  formato IR de OpenVINO (carga más rápida), pesos en float32
    rfdetr-nano.json      metadatos: resolución, índice de la clase persona, licencia, sha256

Los pesos preentrenados se descargan dentro de `<out>/weights/` (no en la carpeta de usuario).
Solo se permiten los tamaños con licencia Apache-2.0 (nano, small, medium, base).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import analytics  # noqa: F401  (bloquea PyAV y la telemetría de OpenVINO antes de importar nada)
from analytics.detector import COCO_PERSON_SLOT, MODEL_SPECS

log = logging.getLogger("analytics.tools.export_model")

DEFAULT_OUT = Path(__file__).resolve().parents[2] / "models"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export(model: str, out_dir: Path, formats: list[str]) -> dict[str, object]:
    if model not in MODEL_SPECS:
        raise SystemExit(f"Modelo no permitido: {model}. Opciones: {', '.join(MODEL_SPECS)}")
    spec = MODEL_SPECS[model]
    out_dir.mkdir(parents=True, exist_ok=True)
    weights = out_dir / "weights"
    weights.mkdir(exist_ok=True)
    os.environ["RF_HOME"] = str(weights)   # que rfdetr descargue aquí y no en ~/.roboflow
    try:
        import rfdetr
    except ImportError as exc:
        raise SystemExit("Falta el extra «export» (rfdetr, torch, onnx): pip install -e .[export]") from exc

    t0 = time.perf_counter()
    net = getattr(rfdetr, spec.rfdetr_class)()
    resolution = int(net.model.resolution)
    onnx_path = out_dir / f"{model}.onnx"
    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        produced = Path(net.export(output_dir=tmp, format="onnx", verbose=False))
        shutil.move(str(produced), onnx_path)
    log.info("ONNX: %s (%.1f MB)", onnx_path, onnx_path.stat().st_size / 1e6)

    meta: dict[str, object] = {
        "model": model, "source": f"rfdetr {getattr(rfdetr, '__version__', '')} {spec.rfdetr_class}".strip(),
        "license": "Apache-2.0", "resolution": resolution, "person_class_index": COCO_PERSON_SLOT,
        "input": "input [1,3,R,R] RGB float32, normalizado ImageNet", "outputs": ["dets (cxcywh 0..1)", "labels (logits)"],
        "onnx_sha256": _sha256(onnx_path), "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if "openvino" in formats:
        import openvino as ov

        core = ov.Core()
        ir = core.read_model(str(onnx_path))
        xml = out_dir / f"{model}.xml"
        ov.save_model(ir, str(xml), compress_to_fp16=False)
        meta["openvino_xml_sha256"] = _sha256(xml)
        meta["openvino_bin_sha256"] = _sha256(xml.with_suffix(".bin"))
        log.info("OpenVINO IR: %s", xml)
    if "onnx" not in formats:
        meta["onnx_note"] = "ONNX conservado: lo necesita ONNX Runtime"
    (out_dir / f"{model}.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Exportado %s en %.0f s", model, time.perf_counter() - t0)
    return meta


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Exporta RF-DETR a ONNX/OpenVINO para la analítica")
    p.add_argument("--model", action="append", choices=sorted(MODEL_SPECS), help="se puede repetir")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--formats", nargs="+", default=["onnx", "openvino"], choices=["onnx", "openvino"])
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    for m in args.model or ["rfdetr-nano"]:
        export(m, args.out, args.formats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
