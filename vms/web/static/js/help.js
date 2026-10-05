// #help-root: ayuda contextual. Un botón «?» en CADA sección de la página (textos en /static/help/es.json,
// preparados para catalán e inglés), un botón «Ayuda» en la cabecera con las guías «Cómo hacer…» y el
// acceso a los recorridos. Nunca en los muros (/wall/N). Dueño: B6. Contrato: CONTRATO §18.14.
//
// Lo cargan index.html directamente y el resto de páginas a través de los módulos de B6 (health.js,
// bookmarks.js, counts-export.js…), que lo importan: así hay ayuda en todas las páginas sin tocar su HTML.
import { h, clear, me } from "./ops-common.js";
import { canTour, startTour, toursForPage } from "./tours.js";

const LANG = (document.documentElement.lang || "es").slice(0, 2);
let data = null;
let drawer = null;
let lastFocus = null;

async function load() {
  if (data) return data;
  for (const lang of [LANG, "es"]) {
    try {
      const res = await fetch(`/static/help/${lang}.json`, { cache: "no-cache" });
      if (res.ok) {
        data = await res.json();
        return data;
      }
    } catch (err) {
      console.warn("Ayuda no disponible", err);
    }
  }
  return null;
}

function t(key) {
  return (data && data.ui && data.ui[key]) || key;
}

function pageSections() {
  const path = location.pathname;
  return Object.entries(data.sections).filter(([, s]) => s.page === path);
}

// ------------------------------------------------------------------ panel lateral
function ensureDrawer() {
  if (drawer) return drawer;
  const root = document.getElementById("help-root") || document.body.appendChild(h("div", { id: "help-root" }));
  root.hidden = false;
  drawer = h("aside", { class: "help-drawer", role: "dialog", "aria-modal": "false", "aria-labelledby": "help-title",
                        hidden: true });
  drawer.addEventListener("keydown", (e) => { if (e.key === "Escape") closeHelp(); });
  root.append(drawer);
  return drawer;
}

function closeHelp() {
  if (!drawer) return;
  drawer.hidden = true;
  if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
}

function drawerFrame(title, backTo) {
  const d = ensureDrawer();
  clear(d);
  const head = h("div", { class: "help-head" },
    backTo ? h("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: backTo }, "← ", t("back")) : null,
    h("h2", { id: "help-title" }, title),
    h("button", { type: "button", class: "btn btn-ghost btn-sm help-close", onclick: closeHelp }, t("close")));
  const body = h("div", { class: "help-body" });
  d.append(head, body);
  d.hidden = false;
  return body;
}

async function tourButton(name) {
  if (!name || !(await canTour())) return null;
  return h("button", { type: "button", class: "btn btn-sm btn-primary", onclick: () => { closeHelp(); startTour(name); } },
    t("tour"));
}

/** Abre la ayuda de una sección. */
export async function openHelp(key) {
  await load();
  const s = data && data.sections[key];
  if (!s) return;
  lastFocus = document.activeElement;
  const body = drawerFrame(s.title, null);
  for (const line of s.intro || []) body.append(h("p", {}, line));
  if (s.buttons && s.buttons.length) {
    body.append(h("h3", {}, t("buttons")),
      h("dl", { class: "help-buttons" }, s.buttons.flatMap((b) => [h("dt", {}, b.label), h("dd", {}, b.text)])));
  }
  if (s.steps && s.steps.length) body.append(h("h3", {}, t("steps")), h("ol", {}, s.steps.map((x) => h("li", {}, x))));
  if (s.tasks && s.tasks.length) {
    body.append(h("h3", {}, t("tasks")), h("ul", { class: "help-tasks" }, s.tasks.filter((id) => data.tasks[id]).map((id) =>
      h("li", {}, h("button", { type: "button", class: "help-link", onclick: () => openTask(id, () => openHelp(key)) },
        data.tasks[id].title)))));
  }
  const tb = await tourButton(s.tour);
  const foot = h("div", { class: "help-foot" }, tb,
    h("button", { type: "button", class: "btn btn-sm", onclick: () => openGuide() }, t("guide")));
  body.append(foot);
  drawer.querySelector(".help-close").focus();
}

export async function openTask(id, backTo = null) {
  await load();
  const task = data && data.tasks[id];
  if (!task) return;
  const body = drawerFrame(task.title, backTo || (() => openGuide()));
  body.append(h("ol", { class: "help-steps" }, task.steps.map((x) => h("li", {}, x))));
  if (task.page !== location.pathname) {
    body.append(h("p", {}, h("a", { class: "btn btn-sm", href: task.page }, "Ir a esa pantalla")));
  }
  drawer.querySelector(".help-close").focus();
}

/** Índice: ayuda de esta pantalla, guías y recorridos. */
export async function openGuide() {
  await load();
  if (!data) return;
  lastFocus = lastFocus || document.activeElement;
  const body = drawerFrame(t("help"), null);
  const secs = pageSections();
  if (secs.length) {
    body.append(h("h3", {}, "En esta pantalla"), h("ul", { class: "help-tasks" }, secs.map(([k, s]) =>
      h("li", {}, h("button", { type: "button", class: "help-link", onclick: () => openHelp(k) }, s.title)))));
  }
  body.append(h("h3", {}, t("tasks")), h("ul", { class: "help-tasks" }, Object.entries(data.tasks).map(([id, task]) =>
    h("li", {}, h("button", { type: "button", class: "help-link", onclick: () => openTask(id) }, task.title)))));
  if (await canTour()) {
    const names = { panel: "Panel de control", monitores: "Monitores", reproduccion: "Reproducción",
                    analitica: "Analítica", estado: "Estado del sistema" };
    body.append(h("h3", {}, "Recorridos de 1 minuto"), h("ul", { class: "help-tasks" }, Object.entries(names).map(([k, label]) =>
      h("li", {}, h("button", { type: "button", class: "help-link", onclick: () => { closeHelp(); startTour(k); } },
        label, toursForPage().includes(k) ? " (esta pantalla)" : "")))));
    if (location.pathname === "/") {
      body.append(h("p", {}, h("button", { type: "button", class: "btn btn-sm btn-primary", onclick: () => {
        closeHelp();
        document.dispatchEvent(new CustomEvent("vms:open-wizard"));
      } }, "Primeros pasos (asistente)")));
    }
  }
  drawer.querySelector(".help-close").focus();
}

// ------------------------------------------------------------------ botones «?»
function helpButton(key, title) {
  return h("button", { type: "button", class: "help-btn", dataset: { help: key },
                       "aria-label": `${t("help_button")}: ${title}`, title: `${t("help")}: ${title}`,
                       onclick: (e) => { e.preventDefault(); e.stopPropagation(); openHelp(key); } }, "?");
}

function attach() {
  if (!data) return;
  for (const [key, s] of pageSections()) {
    if (!s.anchor) continue;
    const anchor = document.querySelector(s.anchor);
    if (!anchor) continue;
    const scope = anchor.closest("section, .card, dialog, header, .page-head, form, main") || anchor.parentElement;
    if (scope && scope.querySelector(`.help-btn[data-help="${CSS.escape(key)}"]`)) continue;
    if (/^H[1-6]$/.test(anchor.tagName) || anchor.tagName === "LABEL") anchor.append(" ", helpButton(key, s.title));
    else anchor.append(helpButton(key, s.title));
  }
  const head = document.querySelector(".page-head");
  if (head && !head.querySelector(".help-global")) {
    const btn = h("button", { type: "button", class: "btn help-global", onclick: () => { lastFocus = btn; openGuide(); } },
      h("span", { class: "help-global-ico", "aria-hidden": "true" }, "?"), " ", t("help"));
    // fuera de «.actions»: en el panel esas acciones son solo para administradores y la ayuda es para todos
    head.append(h("div", { class: "help-global-wrap" }, btn));
  }
}

async function init() {
  if (location.pathname.startsWith("/wall") || location.pathname.startsWith("/login") ||
      location.pathname.startsWith("/setup")) return;
  try {
    const u = await me();
    if (!u || u.kiosk) return;
  } catch {
    return;
  }
  if (!(await load())) return;
  attach();
  // Las secciones de otros módulos y los diálogos se pintan más tarde: se engancha la ayuda al aparecer.
  let pending = false;
  new MutationObserver(() => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; attach(); });
  }).observe(document.querySelector("main") || document.body, { childList: true, subtree: true });
  document.addEventListener("keydown", (e) => {
    if (e.key === "F1") { e.preventDefault(); openGuide(); }
  });
}

window.__vmsHelp = { openHelp, openGuide, openTask, attach };
init();
