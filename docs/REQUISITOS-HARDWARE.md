# Requisitos de hardware

## PC de control (vista en 4 monitores y grabación)

| Pieza | Mínimo | Recomendado | Comentario |
|---|---|---|---|
| CPU | Intel Core i5 de 10.ª gen. / AMD Ryzen 5 3600 | Core i5/i7 de 12.ª gen. o superior | la decodificación va por GPU; la CPU atiende red, servicios y grabación |
| RAM | 16 GB | 32 GB | 4 navegadores con 16 cámaras cada uno consumen memoria |
| GPU | **4 salidas de vídeo** y decodificación H.264 por hardware | NVIDIA T600/T1000 (4× mini-DP), RTX A400/A1000, o Intel Arc A380 con 4 salidas | la decodificación por hardware del navegador (Edge/Chrome) es la clave; la GPU integrada de Intel suele tener 3 salidas como máximo |
| Disco del sistema | SSD 256 GB | NVMe 512 GB | programa, registros y base local |
| Disco de grabación | ver cálculo abajo | discos de vigilancia (WD Purple, Seagate SkyHawk) | escritura continua 24/7 |
| Red | 1 GbE | 2× 1 GbE (una a la VLAN de cámaras, otra a la LAN) | ver [RED.md](RED.md) |
| Sistema | Windows 10 22H2 / 11 Pro 64 bits | Windows 11 Pro | — |
| SAI | — | sí, con apagado ordenado | la grabación fMP4 pierde como mucho 1 s ante un corte, pero el disco agradece no apagarse en caliente |

**Ancho de banda de vista en vivo:** cada celda de un muro usa el subflujo (≈0,5–1 Mbps). 4 muros
de 16 cámaras = 64 subflujos ≈ 32–64 Mbps. Una cámara a pantalla completa pide el flujo principal
(4–8 Mbps). La grabación usa los flujos principales de todas las cámaras.

> Pendiente de medir con hardware real (fase 1 del plan): rendimiento con 16 o más cámaras
> simultáneas en un solo PC. Usa [CHECKLIST-PRUEBAS.md](CHECKLIST-PRUEBAS.md).

## Cálculo de disco por cámara y día

```
GB por día  = bitrate (Mbps) × 86 400 s ÷ 8 ÷ 1 000  =  bitrate (Mbps) × 10,8
Disco total = Σ (GB por día de cada cámara) × días de retención × 1,15 (margen)
```

- **bitrate** = el del **flujo principal** (el que se graba), medio real. Con bitrate variable
  (VBR) la media suele ser el 50–80 % del máximo configurado; mide el real en el NVR o en el panel
  de estado del VMS (bytes recibidos).
- El margen de 1,15 cubre picos y deja libre el umbral del **disk guard** (por defecto el VMS
  borra lo más antiguo si el disco supera el 90 %).

| Cámara (flujo principal) | Bitrate | GB/día | 30 días | 15 cámaras × 30 días (+15 %) |
|---|---|---|---|---|
| 1080p (2 MP) H.264, 25 fps | 4 Mbps | 43,2 | 1,30 TB | 22,4 TB |
| 1080p (2 MP) H.264, 15 fps | 2 Mbps | 21,6 | 0,65 TB | 11,2 TB |
| 1080p (2 MP) H.265 | 2 Mbps | 21,6 | 0,65 TB | 11,2 TB |
| 4 MP (2560×1440) H.264 | 6 Mbps | 64,8 | 1,94 TB | 33,5 TB |
| 4 MP H.264 alta calidad | 8 Mbps | 86,4 | 2,59 TB | 44,7 TB |
| 4 MP H.265 | 4 Mbps | 43,2 | 1,30 TB | 22,4 TB |
| 4 MP H.265 ahorro | 3 Mbps | 32,4 | 0,97 TB | 16,8 TB |

Ejemplo completo: tienda con 12 cámaras 1080p H.264 a 4 Mbps y 4 cámaras 4 MP H.265 a 4 Mbps, 30
días: (12 + 4) × 43,2 GB × 30 × 1,15 = **23,8 TB** → 2 discos de 14 TB (o 3 de 10 TB).

Reducir disco: H.265 en el flujo principal (≈ –40/50 %), bajar fps del principal a 12–15, o
reducir días de retención (Ajustes → Retención).

## Mini PC de tienda para la analítica

La analítica procesa la cámara de la **puerta a 10–15 imágenes por segundo** (con menos, el
seguimiento pierde a quien cruza) y la de **cajas a 1–2 fps**. Corre en CPU con OpenVINO.

| Modelo | CPU | Puerta (12 fps) + 1–2 cajas | Comentario |
|---|---|---|---|
| Intel N100 (4 núcleos, 6 W) | justo | **no recomendado** | sin margen para 12 fps con RF-DETR nano |
| **Intel N150** (4 núcleos) | suficiente para 1 puerta + 2 cajas con RF-DETR nano | mínimo recomendado | 16 GB RAM, NVMe 256 GB |
| Intel Core i5 (12.ª gen. o posterior, p. ej. i5-1235U/1340P) | holgado: 2 puertas + 4 cajas, o modelo small | recomendado si hay varias puertas | 16 GB RAM |

> Nadie ha medido todavía RF-DETR en un N150 con este caso: se mide en la fase 2 con
> `python -m analytics.tools.benchmark` y vídeo real de la tienda antes de comprar en volumen.

Si el mini PC solo hace analítica (el vídeo y la grabación están en otro PC de la tienda), no
necesita disco de grabación. Si además graba, aplica el cálculo de disco anterior.

## Servidor central

Para 147 tiendas: 4 vCPU, 8 GB RAM, 100 GB SSD bastan. La tabla de conteos crece ≈ 1 fila por
regla y minuto: con 3 reglas por tienda, 147 × 3 × 1 440 ≈ 635 000 filas/día (≈ 60–80 MB/día con
índices). Planifica purgar o resumir a horas los minutos de más de 13 meses.
