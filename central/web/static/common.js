/* Utilidades comunes del panel central. Sin dependencias externas. */
"use strict";

const VMS = (() => {
  const nf = new Intl.NumberFormat("es-ES");
  const nf1 = new Intl.NumberFormat("es-ES", { maximumFractionDigits: 1 });

  class ApiError extends Error {
    constructor(status, code, message, details) {
      super(message);
      this.status = status;
      this.code = code;
      this.details = details || {};
    }
  }

  async function api(path, options = {}) {
    const opts = { credentials: "same-origin", ...options };
    opts.headers = { "X-Requested-With": "vms", ...(options.headers || {}) };
    if (options.json !== undefined) {
      opts.body = JSON.stringify(options.json);
      opts.headers["Content-Type"] = "application/json";
      delete opts.json;
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      throw new ApiError(0, "network", "No hay conexión con el panel central.");
    }
    if (res.status === 401 && !options.noRedirect) {
      window.location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
      throw new ApiError(401, "unauthorized", "Inicia sesión para continuar");
    }
    if (res.status === 204) return null;
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
    if (!res.ok) {
      const err = (data && data.error) || {};
      throw new ApiError(res.status, err.code || "error", err.message || ("Error HTTP " + res.status), err.details);
    }
    return data;
  }

  /** Crea un elemento: el("td", {class: "num"}, "texto", otroNodo). Los textos se escapan siempre. */
  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : String(v));
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }

  const num = (v) => (v === null || v === undefined ? "—" : nf.format(v));
  const num1 = (v) => (v === null || v === undefined ? "—" : nf1.format(v));

  function fmtDate(iso, tz, opts) {
    if (!iso) return "—";
    const o = opts || { dateStyle: "short", timeStyle: "short" };
    try { return new Intl.DateTimeFormat("es-ES", { ...o, timeZone: tz || undefined }).format(new Date(iso)); }
    catch (e) { return new Intl.DateTimeFormat("es-ES", o).format(new Date(iso)); }
  }

  function ago(seconds) {
    if (seconds === null || seconds === undefined) return "nunca";
    if (seconds < 60) return "hace " + Math.round(seconds) + " s";
    if (seconds < 3600) return "hace " + Math.round(seconds / 60) + " min";
    if (seconds < 86400) return "hace " + Math.round(seconds / 3600) + " h";
    return "hace " + Math.round(seconds / 86400) + " d";
  }

  function duration(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    const s = Math.round(seconds);
    if (s < 60) return s + " s";
    const m = Math.floor(s / 60);
    if (m < 60) return m + " min";
    return Math.floor(m / 60) + " h " + (m % 60) + " min";
  }

  const STATE_LABEL = { ok: "En línea", degraded: "Con avisos", down: "Caída", unknown: "Sin datos" };
  function stateBadge(state) {
    const s = STATE_LABEL[state] ? state : "unknown";
    return el("span", { class: "badge " + s, role: "status" }, STATE_LABEL[s]);
  }

  function delta(current, previous) {
    if (!previous) return el("span", { class: "muted" }, "—");
    const pct = Math.round(((current - previous) / previous) * 100);
    const cls = pct >= 0 ? "delta-up" : "delta-down";
    return el("span", { class: cls, title: "Respecto al mismo tramo de la semana anterior" },
      (pct >= 0 ? "▲ +" : "▼ ") + pct + " %");
  }

  /* ------------------------------------------------------------------ gráfico de barras */
  /**
   * Barras verticales de una sola serie. data: [{label, value, title}]. Tooltip al pasar el ratón
   * y tabla accesible oculta para lectores de pantalla.
   */
  function barChart(container, data, opts = {}) {
    clear(container);
    container.classList.add("chart");
    const W = 720, H = opts.height || 220, padL = 40, padB = 26, padT = 10, padR = 8;
    const max = Math.max(1, ...data.map((d) => d.value || 0));
    const nice = niceMax(max);
    const innerW = W - padL - padR, innerH = H - padT - padB;
    const bw = data.length ? innerW / data.length : innerW;
    const gap = Math.min(2, bw * 0.2);
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", opts.ariaLabel || "Gráfico de barras");
    const mk = (tag, attrs, text) => {
      const n = document.createElementNS(ns, tag);
      for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
      if (text !== undefined) n.textContent = text;
      svg.appendChild(n);
      return n;
    };
    for (let i = 0; i <= 4; i++) {
      const v = (nice / 4) * i, y = padT + innerH - (v / nice) * innerH;
      mk("line", { x1: padL, x2: W - padR, y1: y, y2: y, class: i === 0 ? "axis" : "grid" });
      mk("text", { x: padL - 6, y: y + 4, "text-anchor": "end", class: "tick" }, nf.format(Math.round(v)));
    }
    const tip = el("div", { class: "tooltip" });
    const every = Math.max(1, Math.ceil(data.length / (opts.maxLabels || 12)));
    data.forEach((d, i) => {
      const v = d.value || 0;
      const h = (v / nice) * innerH;
      const x = padL + i * bw + gap / 2;
      const w = Math.max(1, bw - gap);
      const y = padT + innerH - h;
      const r = Math.min(4, w / 2, h);
      // barra con extremo de datos redondeado (4 px) y anclada a la base
      const path = h <= 0 ? "" :
        `M${x},${padT + innerH} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${padT + innerH} Z`;
      if (path) mk("path", { d: path, class: "bar" });
      const hit = mk("rect", { x: padL + i * bw, y: padT, width: bw, height: innerH, class: "hit" });
      hit.addEventListener("mousemove", (ev) => {
        const rect = container.getBoundingClientRect();
        tip.textContent = d.title || `${d.label}: ${nf.format(v)}`;
        tip.style.left = ev.clientX - rect.left + "px";
        tip.style.top = ev.clientY - rect.top + "px";
        tip.style.display = "block";
      });
      hit.addEventListener("mouseleave", () => { tip.style.display = "none"; });
      if (i % every === 0) {
        mk("text", { x: padL + i * bw + bw / 2, y: H - 8, "text-anchor": "middle", class: "tick" }, d.label);
      }
    });
    container.append(svg, tip, srTable(data, opts.valueLabel || "Valor"));
  }

  /** Barras horizontales (comparativa entre sedes), con etiqueta y valor directos. */
  function hbarChart(container, data, opts = {}) {
    clear(container);
    container.classList.add("chart");
    if (!data.length) { container.append(el("div", { class: "empty" }, opts.empty || "Sin datos")); return; }
    const rowH = 26, padL = 170, padR = 70, W = 720;
    const H = data.length * rowH + 8;
    const max = Math.max(1, ...data.map((d) => d.value || 0));
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", opts.ariaLabel || "Comparativa");
    const mk = (tag, attrs, text) => {
      const n = document.createElementNS(ns, tag);
      for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
      if (text !== undefined) n.textContent = text;
      svg.appendChild(n);
      return n;
    };
    data.forEach((d, i) => {
      const y = 4 + i * rowH;
      const w = ((d.value || 0) / max) * (W - padL - padR);
      mk("text", { x: padL - 8, y: y + rowH / 2 + 4, "text-anchor": "end", class: "label" },
        d.label.length > 24 ? d.label.slice(0, 23) + "…" : d.label);
      const bh = rowH - 8, r = Math.min(4, w / 2, bh / 2);
      if (w > 0) {
        mk("path", { class: "bar", d: `M${padL},${y + 2} H${padL + w - r} Q${padL + w},${y + 2} ${padL + w},${y + 2 + r} ` +
          `V${y + 2 + bh - r} Q${padL + w},${y + 2 + bh} ${padL + w - r},${y + 2 + bh} H${padL} Z` });
      }
      mk("text", { x: padL + w + 6, y: y + rowH / 2 + 4, class: "value" }, nf.format(d.value || 0));
      const t = document.createElementNS(ns, "title");
      t.textContent = d.title || `${d.label}: ${nf.format(d.value || 0)}`;
      mk("rect", { x: 0, y, width: W, height: rowH, class: "hit" }).appendChild(t);
    });
    container.append(svg, srTable(data, opts.valueLabel || "Valor"));
  }

  function srTable(data, valueLabel) {
    const t = el("table", { class: "sr-only" },
      el("thead", {}, el("tr", {}, el("th", {}, "Tramo"), el("th", {}, valueLabel))),
      el("tbody", {}, data.map((d) => el("tr", {}, el("td", {}, d.label), el("td", {}, String(d.value ?? 0))))));
    return t;
  }

  function niceMax(v) {
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }

  /* ------------------------------------------------------------------ markdown seguro */
  /** Markdown básico → nodos DOM (títulos, listas, negrita, cursiva, tablas, párrafos). Nunca innerHTML. */
  function markdown(container, text) {
    clear(container);
    const lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
    let list = null, para = [];
    const flushPara = () => {
      if (para.length) { container.append(el("p", {}, inline(para.join(" ")))); para = []; }
    };
    const flushList = () => { if (list) { container.append(list); list = null; } };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      const h = /^(#{1,4})\s+(.*)$/.exec(line);
      const li = /^\s*(?:[-*]|\d+\.)\s+(.*)$/.exec(line);
      if (/^\s*\|.*\|\s*$/.test(line)) {
        flushPara(); flushList();
        const rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(lines[i]); i++; }
        i--;
        container.append(mdTable(rows));
      } else if (h) {
        flushPara(); flushList();
        container.append(el("h" + Math.min(4, h[1].length + 1), {}, inline(h[2])));
      } else if (li) {
        flushPara();
        if (!list) list = el(/^\s*\d+\./.test(line) ? "ol" : "ul", {});
        list.append(el("li", {}, inline(li[1])));
      } else if (!line.trim()) {
        flushPara(); flushList();
      } else {
        flushList();
        para.push(line.trim());
      }
    }
    flushPara(); flushList();
  }

  function inline(text) {
    const out = [];
    const re = /(\*\*([^*]+)\*\*|\*([^*]+)\*|`([^`]+)`)/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) out.push(text.slice(last, m.index));
      if (m[2] !== undefined) out.push(el("strong", {}, m[2]));
      else if (m[3] !== undefined) out.push(el("em", {}, m[3]));
      else out.push(el("code", {}, m[4]));
      last = re.lastIndex;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }

  function mdTable(rows) {
    const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
    const body = rows.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r));
    const [head, ...rest] = body;
    return el("div", { class: "table-wrap" }, el("table", {},
      el("thead", {}, el("tr", {}, cells(head || "").map((c) => el("th", {}, inline(c))))),
      el("tbody", {}, rest.map((r) => el("tr", {}, cells(r).map((c) => el("td", {}, inline(c))))))));
  }

  /* ------------------------------------------------------------------ cabecera */
  async function topbar(active) {
    const bar = document.getElementById("topbar");
    if (!bar) return null;
    let me = null;
    try { me = await api("/api/auth/me"); } catch (e) { return null; }
    const link = (href, text, key) => el("a", { href, "aria-current": active === key ? "page" : null }, text);
    const logout = el("button", { class: "btn", type: "button" }, "Salir");
    logout.addEventListener("click", async () => {
      try { await api("/api/auth/logout", { method: "POST" }); } finally { location.href = "/login"; }
    });
    clear(bar).append(
      el("span", { class: "brand" }, "VMS Multimarca · Central"),
      el("nav", {}, link("/", "Sedes", "index"), me.role === "admin" ? link("/admin", "Administración", "admin") : null),
      el("span", { class: "user" }, me.username + (me.role === "admin" ? " (admin)" : ""), logout));
    return me;
  }

  function showMsg(node, text, kind) {
    node.textContent = text;
    node.className = "msg show " + (kind || "error");
  }

  return { api, ApiError, el, clear, num, num1, fmtDate, ago, duration, stateBadge, delta, barChart, hbarChart,
           markdown, topbar, showMsg };
})();
