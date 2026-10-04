"use strict";
(async () => {
  const { api, el, clear, topbar, showMsg, fmtDate } = VMS;
  const errBox = document.getElementById("error");
  const me = await topbar("admin");
  if (me && me.role !== "admin") { showMsg(errBox, "Solo un administrador puede ver esta página."); return; }
  const ROLE = { admin: "Administrador", operator: "Operador" };

  async function loadTokens() {
    const rows = await api("/api/site-tokens");
    clear(document.getElementById("tokens")).append(rows.length ? el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Sede"), el("th", {}, "Creado"), el("th", {}, ""))),
      el("tbody", {}, rows.map((r) => el("tr", {}, el("td", {}, r.site_id), el("td", {}, fmtDate(r.created_at)),
        el("td", {}, el("button", { class: "btn danger", type: "button", onclick: async () => {
          if (!confirm(`¿Revocar el token de ${r.site_id}? La sede dejará de poder enviar su estado.`)) return;
          try { await api("/api/site-tokens/" + encodeURIComponent(r.site_id), { method: "DELETE" }); await loadTokens(); }
          catch (e) { showMsg(errBox, e.message); }
        } }, "Revocar")))))) : el("div", { class: "empty" }, "Ninguna sede tiene token todavía"));
  }

  async function loadUsers() {
    const rows = await api("/api/users");
    clear(document.getElementById("users")).append(el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Usuario"), el("th", {}, "Rol"), el("th", {}, "Activo"),
        el("th", {}, "Último acceso"), el("th", {}, ""))),
      el("tbody", {}, rows.map((u) => {
        const act = async (fn) => { try { await fn(); await loadUsers(); } catch (e) { showMsg(errBox, e.message); } };
        const path = "/api/users/" + encodeURIComponent(u.username);
        return el("tr", {}, el("td", {}, u.username), el("td", {}, ROLE[u.role] || u.role),
          el("td", {}, u.enabled ? "Sí" : "No"), el("td", {}, fmtDate(u.last_login_at)),
          el("td", {},
            el("button", { class: "btn", type: "button", onclick: () => act(() => api(path, { method: "PATCH",
              json: { enabled: !u.enabled } })) }, u.enabled ? "Desactivar" : "Activar"), " ",
            el("button", { class: "btn", type: "button", onclick: () => act(() => api(path, { method: "PATCH",
              json: { role: u.role === "admin" ? "operator" : "admin" } })) },
              u.role === "admin" ? "Pasar a operador" : "Hacer administrador"), " ",
            el("button", { class: "btn", type: "button", onclick: () => {
              const pw = prompt(`Nueva contraseña para ${u.username} (mínimo 8 caracteres):`);
              if (pw) act(() => api(path, { method: "PATCH", json: { password: pw } }));
            } }, "Cambiar contraseña"), " ",
            el("button", { class: "btn danger", type: "button", onclick: () => {
              if (confirm(`¿Borrar el usuario ${u.username}?`)) act(() => api(path, { method: "DELETE" }));
            } }, "Borrar")));
      }))));
  }

  document.getElementById("token-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const id = document.getElementById("site-id").value.trim();
    const out = document.getElementById("token-out");
    try {
      const r = await api("/api/site-tokens/" + encodeURIComponent(id), { method: "POST" });
      clear(out).append(el("div", {}, `Token de ${r.site_id}. Cópialo ahora: no se vuelve a mostrar.`),
        el("div", { class: "token" }, r.token));
      out.className = "msg show info";
      await loadTokens();
    } catch (e) { showMsg(out, e.message); }
  });

  document.getElementById("user-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    try {
      await api("/api/users", { method: "POST", json: { username: document.getElementById("new-user").value.trim(),
        password: document.getElementById("new-pass").value, role: document.getElementById("new-role").value } });
      ev.target.reset();
      await loadUsers();
    } catch (e) {
      const fields = (e.details && e.details.fields) || [];
      showMsg(errBox, fields.length ? fields.map((f) => f.msg).join(". ") : e.message);
    }
  });

  try { await Promise.all([loadTokens(), loadUsers()]); } catch (e) { showMsg(errBox, e.message); }
})();
