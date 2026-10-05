// #notifications-root (status.html): avisos por correo y webhook con reglas, agrupación y «Probar».
// Solo administradores. Las contraseñas y secretos nunca vuelven del servidor. Dueño: B6. CONTRATO §18.10.
import { get, put, post } from "./api.js";
import { toast, toastError, busy, showFieldErrors, clearFieldErrors } from "./ui.js";
import { h, clear, me, section, emptyState, fmtWhen, statusPill } from "./ops-common.js";
import "./help.js";

const root = document.getElementById("notifications-root");
const KINDS = {
  camera_down: "Cámara sin vídeo", tamper: "Cámara tapada, movida o desenfocada", clock_skew: "Hora desajustada",
  disk: "Disco", retention_forecast: "Previsión de días de grabación", recording_gap: "Huecos de grabación",
  update_failed: "Actualización fallida",
};
const CHANNELS = { email: "Correo", webhook: "Webhook", telegram: "Telegram" };

function field(label, input, hint) {
  return h("div", { class: "field" }, h("label", {}, label, input), hint ? h("small", {}, hint) : null);
}

function ruleEditor(rule, onRemove) {
  const kinds = h("div", { class: "ops-checks" }, Object.entries(KINDS).map(([k, label]) =>
    h("label", { class: "check" }, h("input", { type: "checkbox", value: k, checked: rule.kinds.includes(k), dataset: { r: "kind" } }), label)));
  const channels = h("div", { class: "ops-checks" }, Object.entries(CHANNELS).map(([k, label]) =>
    h("label", { class: "check" }, h("input", { type: "checkbox", value: k, checked: rule.channels.includes(k), dataset: { r: "channel" } }), label)));
  const sev = h("select", { class: "input", dataset: { r: "severity" } },
    [["info", "Todo (también informativos)"], ["warning", "Avisos y graves"], ["critical", "Solo graves"]].map(([v, t]) =>
      h("option", { value: v, selected: rule.min_severity === v }, t)));
  const group = h("input", { class: "input mono", type: "number", min: "0", max: "3600", value: String(rule.group_seconds), dataset: { r: "group" } });
  const q1 = h("input", { class: "input mono", type: "time", value: rule.quiet_hours ? rule.quiet_hours[0] : "", dataset: { r: "q1" } });
  const q2 = h("input", { class: "input mono", type: "time", value: rule.quiet_hours ? rule.quiet_hours[1] : "", dataset: { r: "q2" } });
  return h("fieldset", { class: "card card-pad ops-rule" },
    h("legend", {}, "Regla"),
    h("div", { class: "field" }, h("span", {}, "Qué avisos"), kinds),
    h("div", { class: "field" }, h("span", {}, "Por qué canales"), channels),
    h("div", { class: "form-grid" },
      field("Desde qué gravedad ", sev),
      field("Agrupar avisos parecidos durante (segundos) ", group, "0 = enviar cada aviso al momento"),
      field("Horas de silencio: desde ", q1, "Lo grave se envía siempre"),
      field("hasta ", q2)),
    h("button", { type: "button", class: "btn btn-sm btn-danger", onclick: onRemove }, "Quitar regla"));
}

function readRule(fs) {
  const q1 = fs.querySelector("[data-r=q1]").value;
  const q2 = fs.querySelector("[data-r=q2]").value;
  return {
    kinds: [...fs.querySelectorAll("[data-r=kind]:checked")].map((x) => x.value),
    channels: [...fs.querySelectorAll("[data-r=channel]:checked")].map((x) => x.value),
    min_severity: fs.querySelector("[data-r=severity]").value,
    group_seconds: Number(fs.querySelector("[data-r=group]").value) || 0,
    quiet_hours: q1 && q2 ? [q1, q2] : null,
    attach_snapshot: false,
  };
}

async function renderLog(box) {
  clear(box);
  let log = [];
  try { log = await get("/api/notifications/log"); } catch (err) { box.append(h("p", { class: "form-error" }, err.message)); return; }
  if (!log.length) {
    box.append(emptyState({ title: "Todavía no se ha enviado ningún aviso",
      text: "Aquí verás cada aviso enviado, por qué canal y si llegó. Pulsa «Probar» para enviar uno de prueba." }));
    return;
  }
  box.append(h("div", { class: "card table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, ["Cuándo", "Aviso", "Canal", "Resultado"].map((x) => h("th", { scope: "col" }, x)))),
    h("tbody", {}, log.slice(0, 50).map((r) => h("tr", {},
      h("td", { class: "small" }, fmtWhen(r.at)),
      h("td", {}, r.title_es, r.grouped > 1 ? h("span", { class: "muted small" }, ` (${r.grouped} agrupados)`) : null),
      h("td", {}, CHANNELS[r.channel] || r.channel),
      h("td", {}, r.ok ? statusPill("ok", "Enviado") : statusPill("critical", "Falló"),
        r.error ? h("div", { class: "muted small" }, r.error) : null)))))));
}

async function main() {
  if (!root) return;
  let u;
  try { u = await me(); } catch { return; }
  if (!u || u.kiosk || u.role !== "admin") return;
  const body = section(root, { id: "h-ops-notify", title: "Avisos por correo y webhook",
    subtitle: "Agrupados para no saturar y, por defecto, sin imágenes" });
  let cfg;
  try { cfg = await get("/api/notifications/settings"); } catch (err) { body.append(h("p", { class: "form-error" }, err.message)); return; }

  const form = h("form", { class: "card card-pad ops-notify-form", novalidate: true });
  const i = (name, attrs = {}) => h("input", { class: "input", name, ...attrs });
  const emailOn = h("input", { type: "checkbox", name: "email_enabled", checked: cfg.email_enabled });
  const webhookOn = h("input", { type: "checkbox", name: "webhook_enabled", checked: cfg.webhook_enabled });
  const smtpPw = i("smtp_password", { type: "password", autocomplete: "new-password",
    placeholder: cfg.has_smtp_password ? "•••••• (guardada; escribe para cambiarla)" : "" });
  const secretOut = h("div", { role: "status" });
  const rulesBox = h("div", { class: "ops-rules" });
  const rules = cfg.rules.length ? cfg.rules : [{ kinds: ["camera_down", "tamper"], channels: [], min_severity: "warning", group_seconds: 120, quiet_hours: null }];
  const addRule = (r) => { const fs = ruleEditor(r, () => fs.remove()); rulesBox.append(fs); };
  rules.forEach(addRule);

  form.append(
    h("h3", {}, "Correo"),
    h("label", { class: "check" }, emailOn, " Enviar avisos por correo"),
    h("div", { class: "form-grid" },
      field("Servidor SMTP ", i("smtp_host", { value: cfg.smtp_host, placeholder: "smtp.tuempresa.es" })),
      field("Puerto ", i("smtp_port", { type: "number", value: String(cfg.smtp_port), class: "input mono" }), "587 con STARTTLS · 465 cifrado"),
      field("Usuario ", i("smtp_username", { value: cfg.smtp_username, autocomplete: "off" })),
      field("Contraseña ", smtpPw, "Se guarda en el almacén de contraseñas, nunca en la configuración."),
      field("Remitente ", i("email_from", { value: cfg.email_from, placeholder: "vms@tienda.es" })),
      field("Destinatarios ", i("email_to", { value: cfg.email_to.join(", "), placeholder: "mantenimiento@empresa.es, …" }), "Separados por comas")),
    h("label", { class: "check" }, h("input", { type: "checkbox", name: "smtp_starttls", checked: cfg.smtp_starttls }), " Usar STARTTLS"),
    h("h3", {}, "Webhook"),
    h("label", { class: "check" }, webhookOn, " Enviar avisos a un webhook (JSON firmado con HMAC-SHA256 en la cabecera X-VMS-Signature)"),
    field("Dirección ", i("webhook_url", { value: cfg.webhook_url, placeholder: "https://receptor.ejemplo/vms" })),
    h("p", { class: "small" }, cfg.has_webhook_secret ? "✔ Hay un secreto de firma guardado." : "⚠ Falta el secreto de firma: sin él no se envía nada."),
    h("div", { class: "ops-row" },
      h("button", { type: "button", class: "btn btn-sm", onclick: async (e) => {
        await busy(e.currentTarget, async () => {
          try {
            const r = await put("/api/notifications/secrets", { generate_webhook_secret: true });
            clear(secretOut).append(h("div", { class: "banner info" }, "Secreto nuevo (cópialo ahora en el receptor; no se volverá a enseñar): ",
              h("code", { class: "mono ops-secret" }, r.webhook_secret)));
          } catch (err) { toastError(err, "No se pudo generar el secreto"); }
        });
      } }, "Generar secreto")),
    secretOut,
    h("h3", {}, "Reglas"), rulesBox,
    h("button", { type: "button", class: "btn btn-sm", onclick: () => addRule({ kinds: ["camera_down"], channels: [], min_severity: "warning", group_seconds: 120 }) },
      "+ Añadir regla"),
    h("p", { class: "form-error", role: "alert", hidden: true }),
    h("div", { class: "ops-row ops-notify-foot" },
      h("button", { type: "submit", class: "btn btn-primary" }, "Guardar"),
      h("button", { type: "button", class: "btn", dataset: { test: "email" } }, "Probar correo"),
      h("button", { type: "button", class: "btn", dataset: { test: "webhook" } }, "Probar webhook")));

  const logBox = h("div", {});
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    clearFieldErrors(form);
    const err = form.querySelector(".form-error");
    err.hidden = true;
    const fd = new FormData(form);
    const body = {
      email_enabled: emailOn.checked, smtp_host: fd.get("smtp_host").trim(), smtp_port: Number(fd.get("smtp_port")) || 587,
      smtp_starttls: form.querySelector("[name=smtp_starttls]").checked, smtp_username: fd.get("smtp_username").trim(),
      email_from: fd.get("email_from").trim(),
      email_to: fd.get("email_to").split(",").map((x) => x.trim()).filter(Boolean),
      webhook_enabled: webhookOn.checked, webhook_url: fd.get("webhook_url").trim(),
      rules: [...rulesBox.querySelectorAll("fieldset")].map(readRule),
    };
    await busy(e.submitter, async () => {
      try {
        await put("/api/notifications/settings", body);
        if (smtpPw.value) { await put("/api/notifications/secrets", { smtp_password: smtpPw.value }); smtpPw.value = ""; }
        toast("Avisos guardados", "ok");
      } catch (ex) {
        if (!showFieldErrors(form, ex)) { err.textContent = ex.message; err.hidden = false; }
        else { err.textContent = ex.message; err.hidden = false; }
      }
    });
  });
  form.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-test]");
    if (!b) return;
    await busy(b, async () => {
      try {
        const r = await post("/api/notifications/test", { channel: b.dataset.test });
        toast(r.ok ? `Aviso de prueba enviado por ${CHANNELS[r.channel]}` : `No se pudo enviar: ${r.error}`, r.ok ? "ok" : "bad", 8000);
        renderLog(logBox);
      } catch (ex) { toastError(ex, "Prueba"); }
    });
  });
  body.append(form, h("h3", {}, "Últimos avisos"), logBox);
  renderLog(logBox);
}

main();
