"""Celdas de texto seguras para CSV que se abren en Excel (inyección de fórmulas, CWE-1236).

Una celda que empieza por `=`, `+`, `-`, `@`, tabulador o retorno de carro se interpreta como fórmula. Los
nombres de tienda y de cámara y las frases de problemas llegan de la configuración de cada tienda o de su
latido, así que se tratan como texto no fiable: se les antepone un apóstrofo («'»), que Excel no muestra.
Solo para columnas de TEXTO: las numéricas (p. ej. un desfase «-3,2») se escriben tal cual.
"""
from __future__ import annotations

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def text_cell(value: object) -> str:
    s = "" if value is None else str(value)
    return "'" + s if s.startswith(_FORMULA_START) else s
