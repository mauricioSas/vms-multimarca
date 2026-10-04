# Analítica de tienda

Cuenta personas en la puerta y vigila la cola de cajas. Escribe conteos por minuto en PostgreSQL,
avisa por Telegram y genera el informe semanal. Contrato de interfaces: `docs/CONTRATO.md` §8.
Privacidad: `analytics/RGPD.md`.

## Cómo funciona (en una frase por pieza)

| Pieza | Archivo | Qué hace |
|---|---|---|
| Configuración | `config.py` | Pide cámaras y reglas al backend cada 30 s (con caché local si no responde) |
| Vídeo | `video.py` | Lee el subflujo desde MediaMTX por TCP; se queda solo con la última imagen; reconecta 1-2-5-10-30 s |
| Detector | `detector.py` | RF-DETR (Apache) con OpenVINO u ONNX Runtime; solo la clase persona |
| Seguimiento y conteo | `counting.py` | ByteTrack + línea (entradas/salidas con anti-rebote) + zona (ocupación) |
| Cámara | `pipeline.py` | Un hilo por cámara a los fps configurados (puerta 10-15, cajas 1-2) |
| Minutos | `aggregation.py` | Suma por minuto UTC y lo cierra 5 s después de terminar |
| Alertas | `alerts.py`, `telegram.py` | «≥ X personas durante ≥ Y s», cooldown, fin tras 30 s por debajo |
| Base de datos | `storage.py` | Cola en disco + upsert idempotente en PostgreSQL |
| Servicio | `service.py`, `__main__.py` | Orquesta todo y escribe `<datos>/analytics/status.json` cada 10 s |
| Informe | `reports/` | Cifras en SQL → texto (proveedor LLM o plantilla) → Markdown/HTML |

## Preparar el modelo (una vez, en la máquina de preparación)

```bash
pip install --no-deps -r requirements-export.txt   # rfdetr + torch + onnx (no va a la tienda)
python -m analytics.tools.export_model --model rfdetr-nano --model rfdetr-small
```
Deja en `models/` los archivos `rfdetr-<tamaño>.onnx`, `.xml/.bin` (OpenVINO) y `.json`. Esa carpeta
se copia a cada tienda. Solo se permiten nano/small/medium/base (Apache-2.0).

## Ejecutar en la tienda

```bash
python -m analytics check     # diagnóstico: modelo, motor, PostgreSQL, backend, Telegram
python -m analytics           # servicio (lo arranca systemd / el servicio de Windows)
python -m analytics run --config config.json --seconds 120   # configuración fija, para pruebas
```

## Medir el rendimiento del equipo

```bash
python -m analytics.tools.benchmark --video tests/assets/people-walking-h264.mp4 \
    --model rfdetr-nano --model rfdetr-small --backend openvino --backend onnxruntime
```

Medido en el Mac de desarrollo (Apple M1 Pro, CPU, 60 imágenes 1280x720, 4-oct-2026):

| Modelo | Motor | p50 ms | p95 ms | Imágenes/s (1 cámara) |
|---|---|---:|---:|---:|
| nano | OpenVINO (f32) | 81 | 92 | 12,3 |
| nano | ONNX Runtime | 119 | 120 | 8,4 |
| small | OpenVINO (f32) | 150 | 180 | 6,7 |
| small | ONNX Runtime | 216 | 220 | 4,6 |

En la prueba de extremo a extremo (vídeo → NVR simulado → MediaMTX → analítica, con ffmpeg y
MediaMTX en la misma máquina) la cámara de puerta configurada a 10 fps procesó **7,8 fps reales**
con nano/OpenVINO (p50 91 ms). **Pendiente de medir en el mini PC Intel N150/i5** de tienda: es la
cifra que decide cuántas cámaras de puerta caben por equipo.

## Informe semanal

```bash
python -m analytics.reports --site site-bcn-001 --last-week --out informes/
python -m analytics.reports --all-sites --week 2026-09-28 --no-llm --out informes/
```
Proveedor configurable con `VMS_LLM_PROVIDER` (`anthropic` | `none`), `VMS_LLM_MODEL` y
`VMS_LLM_API_KEY`. Sin clave, o si el proveedor falla o cita cifras que no están en los datos, el
texto se genera con una plantilla fija; el informe sale siempre.

## Pruebas

```bash
.venv/bin/python -m pytest tests/analytics -m "not e2e"     # rápidas (≈ 12 s)
.venv/bin/python -m pytest tests/analytics                   # incluye vídeo real por RTSP (≈ 45 s)
```
