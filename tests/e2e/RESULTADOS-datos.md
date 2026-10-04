# Prueba de sistema — datos medidos (generado automáticamente)

Generado por `python -m tests.e2e.system_check` · inicio 2026-10-04T19:32:01.463595+00:00 · fin 2026-10-04T19:41:30.356686+00:00 · plataforma darwin arm64

| Paso | Descripción | Resultado | Duración |
|---|---|---|---:|
| a | Simulador: 4 cámaras (2 estilo Hikvision, 2 estilo Dahua) + 1 cámara con vídeo de personas | **OK** | 0 s |
| b | Arranque del sistema, login, alta de cámaras por la API y muros 1-4 con layouts distintos | **OK** | 2 s |
| c | Playwright: /wall/1 a /wall/4 con vídeo WebRTC en todas las celdas asignadas | **OK** | 17 s |
| d | Grabación de ~150 s, línea de tiempo, MP4 válido y reproducción en la interfaz | **OK** | 153 s |
| e | Corte de 20 s de una cámara simulada (principal y subflujo) y recuperación sola | **OK** | 40 s |
| f | MediaMTX matado (SIGKILL): el supervisor del backend lo relanza y todo vuelve | **OK** | 29 s |
| g | Analítica real (RF-DETR nano): conteos por minuto en PostgreSQL, alerta de cola por Telegram simulado e informe semanal con plantilla | **OK** | 0 s |
| h | Panel central: recibe el latido (directo a PostgreSQL y por el agente HTTP) y muestra la sede | **OK** | 3 s |
| i | Carga: 16 flujos simulados en un muro 4x4 durante 300 s (CPU y memoria de backend, MediaMTX, navegador) | **OK** | 311 s |
| z | Parada ordenada: sin procesos huérfanos y sin contraseñas en disco ni registros | **OK** | 12 s |

## a) Simulador: 4 cámaras (2 estilo Hikvision, 2 estilo Dahua) + 1 cámara con vídeo de personas

```json
{
  "devices": {
    "hik1": {
      "vendor": "hikvision",
      "channels": 2,
      "rtsp_port": 58725,
      "native_main": "/Streaming/Channels/101",
      "native_sub": "/Streaming/Channels/102"
    },
    "dah1": {
      "vendor": "dahua",
      "channels": 2,
      "rtsp_port": 58726,
      "native_main": "/cam/realmonitor?channel=1&subtype=0",
      "native_sub": "/cam/realmonitor?channel=1&subtype=1"
    },
    "door1": {
      "vendor": "generic",
      "channels": 1,
      "rtsp_port": 58727,
      "native_main": "/ch1/main",
      "native_sub": "/ch1/sub"
    }
  },
  "hik_http_api_port": 58740,
  "dah_http_api_port": 58741,
  "flows_ready": 10
}
```

## b) Arranque del sistema, login, alta de cámaras por la API y muros 1-4 con layouts distintos

```json
{
  "backend_ready_s": 0.7,
  "login_bad_password": 401,
  "login_ok": true,
  "devices": {
    "hikvision": {
      "test_ok": true,
      "rtsp_ok": true,
      "model": "DS-7608NI-K2/8P",
      "cameras": 2,
      "import_error": null
    },
    "dahua": {
      "test_ok": true,
      "rtsp_ok": true,
      "model": "DHI-NVR4208-8P-4KS2/L",
      "cameras": 2,
      "import_error": null
    },
    "generic": {
      "cameras": 1
    }
  },
  "cameras": [
    {
      "id": "cam-352cad5a",
      "name": "Entrada",
      "vendor": "hikvision"
    },
    {
      "id": "cam-c5f0528b",
      "name": "Cajas",
      "vendor": "hikvision"
    },
    {
      "id": "cam-8b24c8e3",
      "name": "Parking",
      "vendor": "dahua"
    },
    {
      "id": "cam-984fe70c",
      "name": "Muelle",
      "vendor": "dahua"
    },
    {
      "id": "cam-b5636fd5",
      "name": "Puerta principal",
      "vendor": "generic"
    }
  ],
  "walls": {
    "1": {
      "grid": 4,
      "cameras": 4
    },
    "2": {
      "grid": 1,
      "cameras": 1
    },
    "3": {
      "grid": 9,
      "cameras": 5
    },
    "4": {
      "grid": 16,
      "cameras": 5
    }
  },
  "all_online_recording_s": 1.2,
  "status": "ok"
}
```

## c) Playwright: /wall/1 a /wall/4 con vídeo WebRTC en todas las celdas asignadas

```json
{
  "wall1": {
    "cells": 4,
    "all_playing_after_s": 2.5,
    "videoWidth": [
      320
    ],
    "currentTime_advance_2.5s": [
      2.42,
      2.42,
      2.42,
      2.42
    ],
    "peer_connections_open": 4
  },
  "wall2": {
    "cells": 1,
    "all_playing_after_s": 2.5,
    "videoWidth": [
      1280
    ],
    "currentTime_advance_2.5s": [
      2.53
    ],
    "peer_connections_open": 1
  },
  "wall3": {
    "cells": 5,
    "all_playing_after_s": 2.5,
    "videoWidth": [
      320,
      640
    ],
    "currentTime_advance_2.5s": [
      2.47,
      2.5,
      2.5,
      2.5,
      2.4
    ],
    "peer_connections_open": 5
  },
  "wall4": {
    "cells": 5,
    "all_playing_after_s": 2.5,
    "videoWidth": [
      320,
      640
    ],
    "currentTime_advance_2.5s": [
      2.4,
      2.5,
      2.5,
      2.5,
      2.48
    ],
    "peer_connections_open": 5
  },
  "mediamtx_webrtc_sessions": 15,
  "mediamtx_api_anonymous_status": 401,
  "dblclick_main_stream": {
    "stream": "main",
    "videoWidth": 640
  },
  "back_to_grid_all_playing_s": 2.5,
  "js_errors": {}
}
```

## d) Grabación de ~150 s, línea de tiempo, MP4 válido y reproducción en la interfaz

```json
{
  "timelines": {
    "cam-352cad5a": {
      "spans": 1,
      "seconds": 165.0,
      "first_start": "2026-10-04T19:32:06.084257Z"
    },
    "cam-c5f0528b": {
      "spans": 1,
      "seconds": 165.0,
      "first_start": "2026-10-04T19:32:06.091940Z"
    },
    "cam-8b24c8e3": {
      "spans": 1,
      "seconds": 165.0,
      "first_start": "2026-10-04T19:32:06.083822Z"
    },
    "cam-984fe70c": {
      "spans": 1,
      "seconds": 165.0,
      "first_start": "2026-10-04T19:32:06.098193Z"
    },
    "cam-b5636fd5": {
      "spans": 1,
      "seconds": 165.1,
      "first_start": "2026-10-04T19:32:06.708938Z"
    }
  },
  "clip": {
    "http": 200,
    "content_type": "video/mp4",
    "content_disposition": "attachment; filename=\"Entrada_20261004-213236.mp4\"; filename*=UTF-8''Entrada_20261004-213236.mp4",
    "bytes": 3458422,
    "ffprobe_codec": "h264",
    "width": 640,
    "height": 360,
    "frames": "300",
    "duration_s": "30.000000",
    "ffprobe_error": null
  },
  "ui_playback": {
    "t": 2.06188,
    "w": 640,
    "h": 360,
    "paused": false,
    "advanced": 1.52
  }
}
```

## e) Corte de 20 s de una cámara simulada (principal y subflujo) y recuperación sola

```json
{
  "api_detects_cut_s": 0.0,
  "status_during_cut": {
    "status": "degraded",
    "problems": [
      "cámaras sin vídeo"
    ]
  },
  "wall_cell_state_during_cut": "reconnecting",
  "cut_seconds": 20.1,
  "api_recovers_s": 4.6,
  "wall_recovers_s": 3.5,
  "wall_recovers_since_restart_s": 8.7,
  "recording": {
    "spans": 2,
    "gap_seconds": 25.8,
    "recording_after_restart_s": 11.0
  },
  "other_cells_unaffected": true
}
```

## f) MediaMTX matado (SIGKILL): el supervisor del backend lo relanza y todo vuelve

```json
{
  "engine_relaunched_s": 1.3,
  "pids": {
    "before": 30337,
    "after": 30474
  },
  "all_cameras_online_s": 0.0,
  "all_cameras_online_since_kill_s": 1.3,
  "walls_live_since_kill_s": {
    "1": 21.4,
    "2": 23.9,
    "3": 26.4,
    "4": 28.9
  },
  "engine_after": {
    "running": true,
    "restarts": 1
  },
  "old_pid_alive": false
}
```

## g) Analítica real (RF-DETR nano): conteos por minuto en PostgreSQL, alerta de cola por Telegram simulado e informe semanal con plantilla

```json
{
  "analytics_status": {
    "state": "running",
    "fps_target": 10.0,
    "fps_in": 14.99,
    "fps_processed": 8.0,
    "inference_ms_p50": 102.2,
    "inference_ms_p95": 125.6,
    "frames_processed": 1949,
    "reconnects": 2,
    "detector": {
      "model": "rfdetr-nano",
      "backend": "openvino"
    },
    "frame_size": [
      640,
      360
    ],
    "totals": {
      "rule-4e5ac3c8": {
        "in": 144,
        "out": 129
      }
    },
    "last_error": "",
    "stale": false,
    "db": {
      "ok": true,
      "spool_pending": 0,
      "memory_pending": 0,
      "last_error": "",
      "disk_error": ""
    }
  },
  "line_counts_minute": {
    "rows": 3,
    "in": 113,
    "out": 99,
    "first_minute": "2026-10-04 21:32:00+02:00",
    "last_minute": "2026-10-04 21:34:00+02:00",
    "per_minute": [
      [
        "2026-10-04 21:32:00+02:00",
        33,
        28
      ],
      [
        "2026-10-04 21:33:00+02:00",
        41,
        36
      ],
      [
        "2026-10-04 21:34:00+02:00",
        39,
        35
      ]
    ]
  },
  "zone_occupancy_minute": {
    "rows": 3,
    "max_people": 13,
    "samples": 1507,
    "avg_people": 9.78,
    "seconds_over_threshold": 166
  },
  "queue_alerts": {
    "rows": 1,
    "notified": 1,
    "ended": 0,
    "peak_people": 11
  },
  "sites_cameras_rules": {
    "site_name": "Tienda E2E",
    "site_cameras": 5,
    "rules": 2
  },
  "telegram_mock": {
    "messages": 1,
    "token_ok": true,
    "chat_ok": true,
    "first_text": "COLA EN CAJAS · Tienda E2E\nZona «Cola cajas» (Puerta principal): 8 personas en cola desde las 21:32 (aviso a partir de 3).\nConviene abrir otra caja.",
    "has_photo": false
  },
  "rgpd_image_files_outside_recordings": [],
  "weekly_report": {
    "exit_code": 0,
    "stdout": "site-e2e-001: ok (template) → /Users/mauriciosas/Documents/vms-multimarca/.tmp/e2e-system/informes/informe_site-e2e-001_2026-09-28.md · /Users/mauriciosas/Documents/vms-multimarca/.tmp/e2e-system/informes/informe_site-e2e-001_2026-09-28.html",
    "stderr": "INFO analytics.reports: Informe site-e2e-001 2026-09-28 generado (ok, template)",
    "week_start": "2026-09-28",
    "db_row": {
      "status": "ok",
      "provider": "template",
      "markdown_chars": 1725,
      "error": null
    },
    "files": [
      "informe_site-e2e-001_2026-09-28.md",
      "informe_site-e2e-001_2026-09-28.html"
    ],
    "markdown_head": "# Informe semanal · Tienda E2E\n\nSemana 2026-W40 · del 28 de septiembre al 4 de octubre (hora local Europe/Madrid)\n\n## Resumen\n\nEsta semana entraron **113 personas** en la tienda (media de 16,1 al día). No hay datos de la semana anterior para comparar.\n\nEl día con más afluencia fue el domingo 4 de octubre (113 entradas). La franja con más entradas fue la de 21:00–22:00 (113 en toda la semana).\n\nOjo: la cámara de puerta tuvo datos el 0 % del tiempo; las cifras reales pueden ser algo mayores.\n\nColas en cajas. «Cola cajas»: media de 9,8 personas y un máximo de 13; 2,8 minutos por encima del umbral de aviso; la franja más cargada fue 21:00–22:00.\n\nSe envió **1 aviso de cola** (la semana anterior, 0). Pico de 11 personas.\n\n## Entradas por día\n\n| Día | Fecha | Entradas | Salidas |\n|---|---|---:|---:|\n| lunes | 28 de septiembre | 0 | 0 |\n| martes | 29 de septiembre | 0 | 0 |\n| miércoles | 30 de "
  }
}
```

## h) Panel central: recibe el latido (directo a PostgreSQL y por el agente HTTP) y muestra la sede

```json
{
  "direct_heartbeat_seen_s": 0.0,
  "site_direct": {
    "site_id": "site-e2e-001",
    "name": "Tienda E2E",
    "online": true,
    "state": "ok",
    "last_seen": "2026-10-04T19:36:03.658221Z",
    "cameras_total": 5,
    "cameras_online": 5,
    "version": "0.1.0",
    "hostname": "MacBook-Pro-de-Andres.local",
    "analytics_running": true,
    "today": {
      "in": 113,
      "out": 99,
      "alerts": 1
    }
  },
  "agent": {
    "exit_code": 0,
    "token_issued": true,
    "stderr_tail": "",
    "last_seen_before": "2026-10-04T19:36:03.658221Z",
    "last_seen_after": "2026-10-04T19:36:05.005556Z",
    "state_after": "ok",
    "detail_cameras": 5,
    "detail_rules": 2
  },
  "central_counts_today": {
    "site_id": "site-e2e-001",
    "timezone": "Europe/Madrid",
    "range": "today",
    "from": "2026-10-03T22:00:00Z",
    "to": "2026-10-04T19:36:05.069536Z",
    "bucket": "hour",
    "rule_id": null,
    "total": {
      "in": 113,
      "out": 99
    },
    "series": [
      {
        "start": "2026-10-03T22:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-03T23:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T00:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T01:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T02:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T03:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T04:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T05:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T06:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T07:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T08:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T09:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T10:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T11:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T12:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T13:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T14:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T15:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T16:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T17:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T18:00:00Z",
        "in": 0,
        "out": 0
      },
      {
        "start": "2026-10-04T19:00:00Z",
        "in": 113,
        "out": 99
      }
    ]
  },
  "central_ui_text_has_site": true
}
```

## i) Carga: 16 flujos simulados en un muro 4x4 durante 300 s (CPU y memoria de backend, MediaMTX, navegador)

```json
{
  "load_cameras": 16,
  "all_recording_s": 1.2,
  "wall4_all_playing_s": 5.0,
  "start_cells": {
    "playing": 16,
    "of": 16,
    "videoWidth": [
      320
    ]
  },
  "end_cells": {
    "playing": 16,
    "of": 16,
    "frames_decoded_during_test": 48581,
    "dropped_frames_total": 0,
    "reconnects_total": 0
  },
  "peer_connections_open": 16,
  "mediamtx_webrtc_sessions": 16,
  "resources": {
    "backend": {
      "cpu_avg_pct": 0.3,
      "cpu_max_pct": 0.5,
      "rss_start_mb": 62.8,
      "rss_end_mb": 57.6,
      "rss_first_fifth_avg_mb": 59.2,
      "rss_last_fifth_avg_mb": 58.1,
      "rss_slope_mb_per_min": -1.25
    },
    "mediamtx": {
      "cpu_avg_pct": 12.2,
      "cpu_max_pct": 13.1,
      "rss_start_mb": 108.5,
      "rss_end_mb": 135.8,
      "rss_first_fifth_avg_mb": 129.8,
      "rss_last_fifth_avg_mb": 135.6,
      "rss_slope_mb_per_min": 1.36
    },
    "navegador": {
      "cpu_avg_pct": 26.2,
      "cpu_max_pct": 31.1,
      "rss_start_mb": 696.3,
      "rss_end_mb": 693.5,
      "rss_first_fifth_avg_mb": 651.7,
      "rss_last_fifth_avg_mb": 657.9,
      "rss_slope_mb_per_min": 3.07
    },
    "analitica": {
      "cpu_avg_pct": 464.4,
      "cpu_max_pct": 482.8,
      "rss_start_mb": 443.4,
      "rss_end_mb": 448.3,
      "rss_first_fifth_avg_mb": 446.9,
      "rss_last_fifth_avg_mb": 445.9,
      "rss_slope_mb_per_min": 0.11
    },
    "simulador": {
      "cpu_avg_pct": 71.0,
      "cpu_max_pct": 72.0,
      "rss_start_mb": 980.6,
      "rss_end_mb": 948.7,
      "rss_first_fifth_avg_mb": 955.2,
      "rss_last_fifth_avg_mb": 946.2,
      "rss_slope_mb_per_min": -1.91
    }
  },
  "system": {
    "cpu_count": 8,
    "cpu_avg_pct_total": 78.5,
    "mem_used_gb_start": 5.82,
    "mem_used_gb_end": 5.73
  }
}
```

Muestras cada 10 s (t s, CPU % de un núcleo, RSS MB):

| t | backend | mediamtx | navegador | analitica | simulador |
|---:|---|---|---|---|---|
| 10 | 0 % · 63 MB | 13 % · 108 MB | 31 % · 696 MB | 458 % · 443 MB | 71 % · 981 MB |
| 20 | 0 % · 58 MB | 12 % · 133 MB | 31 % · 631 MB | 461 % · 445 MB | 70 % · 959 MB |
| 30 | 0 % · 58 MB | 12 % · 134 MB | 27 % · 643 MB | 457 % · 444 MB | 70 % · 948 MB |
| 40 | 0 % · 58 MB | 12 % · 133 MB | 26 % · 637 MB | 462 % · 446 MB | 71 % · 946 MB |
| 50 | 0 % · 59 MB | 12 % · 136 MB | 26 % · 642 MB | 460 % · 452 MB | 71 % · 950 MB |
| 60 | 0 % · 58 MB | 12 % · 134 MB | 26 % · 661 MB | 480 % · 451 MB | 72 % · 947 MB |
| 70 | 0 % · 58 MB | 12 % · 134 MB | 26 % · 666 MB | 461 % · 443 MB | 71 % · 946 MB |
| 80 | 0 % · 59 MB | 12 % · 135 MB | 26 % · 613 MB | 462 % · 447 MB | 71 % · 948 MB |
| 90 | 0 % · 81 MB | 12 % · 134 MB | 26 % · 610 MB | 482 % · 446 MB | 71 % · 943 MB |
| 100 | 0 % · 80 MB | 12 % · 134 MB | 26 % · 603 MB | 459 % · 442 MB | 71 % · 944 MB |
| 110 | 0 % · 81 MB | 12 % · 136 MB | 26 % · 599 MB | 461 % · 444 MB | 71 % · 948 MB |
| 120 | 0 % · 57 MB | 12 % · 136 MB | 26 % · 645 MB | 459 % · 447 MB | 71 % · 948 MB |
| 130 | 0 % · 58 MB | 12 % · 136 MB | 26 % · 648 MB | 483 % · 449 MB | 71 % · 945 MB |
| 140 | 0 % · 59 MB | 12 % · 135 MB | 26 % · 607 MB | 460 % · 443 MB | 71 % · 946 MB |
| 150 | 0 % · 58 MB | 12 % · 136 MB | 26 % · 610 MB | 457 % · 444 MB | 71 % · 952 MB |
| 160 | 0 % · 58 MB | 12 % · 136 MB | 26 % · 610 MB | 461 % · 445 MB | 71 % · 952 MB |
| 170 | 0 % · 59 MB | 12 % · 136 MB | 26 % · 648 MB | 482 % · 449 MB | 72 % · 952 MB |
| 180 | 0 % · 58 MB | 12 % · 135 MB | 26 % · 667 MB | 458 % · 450 MB | 71 % · 945 MB |
| 190 | 0 % · 57 MB | 12 % · 134 MB | 26 % · 620 MB | 453 % · 440 MB | 71 % · 943 MB |
| 200 | 0 % · 58 MB | 12 % · 135 MB | 26 % · 626 MB | 459 % · 441 MB | 71 % · 948 MB |
| 210 | 0 % · 58 MB | 12 % · 137 MB | 26 % · 626 MB | 479 % · 446 MB | 71 % · 948 MB |
| 220 | 0 % · 59 MB | 12 % · 136 MB | 26 % · 631 MB | 460 % · 447 MB | 71 % · 947 MB |
| 230 | 0 % · 59 MB | 12 % · 137 MB | 26 % · 640 MB | 463 % · 449 MB | 70 % · 950 MB |
| 240 | 0 % · 58 MB | 12 % · 134 MB | 26 % · 661 MB | 476 % · 449 MB | 72 % · 943 MB |
| 250 | 0 % · 58 MB | 12 % · 135 MB | 26 % · 628 MB | 458 % · 446 MB | 71 % · 943 MB |
| 260 | 0 % · 59 MB | 12 % · 137 MB | 25 % · 628 MB | 461 % · 445 MB | 72 % · 947 MB |
| 270 | 0 % · 59 MB | 12 % · 137 MB | 26 % · 662 MB | 466 % · 450 MB | 70 % · 949 MB |
| 280 | 0 % · 58 MB | 12 % · 134 MB | 26 % · 673 MB | 476 % · 444 MB | 72 % · 942 MB |
| 290 | 0 % · 58 MB | 12 % · 135 MB | 26 % · 663 MB | 459 % · 443 MB | 72 % · 948 MB |
| 300 | 0 % · 58 MB | 12 % · 136 MB | 26 % · 694 MB | 458 % · 448 MB | 71 % · 949 MB |

## z) Parada ordenada: sin procesos huérfanos y sin contraseñas en disco ni registros

```json
{
  "analytics_exit_code": 0,
  "central_exit_code": -15,
  "backend_exit_code": -15,
  "mediamtx_orphan": false,
  "secrets_in_files": [],
  "audit_events": {
    "live_main": 3,
    "recording_download": 1,
    "recording_view": 1
  }
}
```
