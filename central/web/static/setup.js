"use strict";
(() => {
  const { api, showMsg } = VMS;
  const form = document.getElementById("form");
  const msg = document.getElementById("msg");
  form.querySelector("button").disabled = false;   // llega desactivado: ver setup.html
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (form.password.value !== form.password2.value) { showMsg(msg, "Las contraseñas no coinciden"); return; }
    try {
      await api("/api/auth/setup", { method: "POST", noRedirect: true,
        json: { username: form.username.value.trim(), password: form.password.value } });
      showMsg(msg, "Administrador creado. Ya puedes entrar.", "info");
      setTimeout(() => { location.href = "/login"; }, 1200);
    } catch (e) {
      const fields = (e.details && e.details.fields) || [];
      showMsg(msg, fields.length ? fields.map((f) => f.msg).join(". ") : e.message);
    }
  });
})();
