// Geometría de reglas de analítica en coordenadas normalizadas 0..1 (x a la derecha, y hacia abajo).
// Misma semántica que LineRule en vms/core/models.py:
//   v = end - start;  s(p) = v.x * (p.y - start.y) - v.y * (p.x - start.x)
//   pasar de s < 0 a s > 0 = ENTRADA (sin invertir). La flecha de entrada apunta a n = (-v.y, v.x).

export const clamp01 = (v) => Math.max(0, Math.min(1, v));
export const round4 = (v) => Math.round(v * 10000) / 10000;

export function normPoint(p) {
  return [round4(clamp01(p[0])), round4(clamp01(p[1]))];
}

/** Lado de la línea en el que cae p (>0 = lado de «dentro» sin invertir). */
export function lineSide(start, end, p) {
  const vx = end[0] - start[0];
  const vy = end[1] - start[1];
  return vx * (p[1] - start[1]) - vy * (p[0] - start[0]);
}

/**
 * Dirección de la flecha de ENTRADA en píxeles (vector unitario) para una línea dibujada en un
 * lienzo de w×h. El signo de s(p) no cambia al escalar los ejes, así que basta con la normal
 * en píxeles. Con invert=true la entrada es la contraria.
 */
export function entryDirectionPx(start, end, w, h, invert = false) {
  const vx = (end[0] - start[0]) * w;
  const vy = (end[1] - start[1]) * h;
  const len = Math.hypot(vx, vy) || 1;
  const sign = invert ? -1 : 1;
  return [(-vy / len) * sign, (vx / len) * sign];
}

export function distToSegment(p, a, b) {
  const vx = b[0] - a[0];
  const vy = b[1] - a[1];
  const len2 = vx * vx + vy * vy;
  let t = len2 ? ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / len2 : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(p[0] - (a[0] + t * vx), p[1] - (a[1] + t * vy));
}

export function pointInPolygon(p, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i];
    const [xj, yj] = poly[j];
    if ((yi > p[1]) !== (yj > p[1]) && p[0] < ((xj - xi) * (p[1] - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

export function centroid(poly) {
  let x = 0;
  let y = 0;
  for (const [px, py] of poly) { x += px; y += py; }
  return poly.length ? [x / poly.length, y / poly.length] : [0.5, 0.5];
}

/** ¿Se cruzan los segmentos ab y cd? (para avisar de polígonos que se cortan a sí mismos) */
function segmentsIntersect(a, b, c, d) {
  const o = (p, q, r) => Math.sign((q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0]));
  return o(a, b, c) !== o(a, b, d) && o(c, d, a) !== o(c, d, b);
}

export function isSelfIntersecting(poly) {
  const n = poly.length;
  if (n < 4) return false;
  for (let i = 0; i < n; i++) {
    const a = poly[i];
    const b = poly[(i + 1) % n];
    for (let j = i + 2; j < n; j++) {
      if (i === 0 && j === n - 1) continue; // lados contiguos por el cierre
      if (segmentsIntersect(a, b, poly[j], poly[(j + 1) % n])) return true;
    }
  }
  return false;
}
