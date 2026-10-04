"use strict";

// Solo rutas de este mismo panel: «//dominio», «/\\dominio» o «https://…» llevarían a otra web tras un
// login real (redirección abierta). Se resuelve contra el origen y se exige que no cambie.
function safeNext(value) {
  if (!value || !value.startsWith("/") || value.includes("\\")) return "/";
  try {
    const url = new URL(value, location.origin);
    return url.origin === location.origin ? url.pathname + url.search + url.hash : "/";
  } catch (e) {
    return "/";
  }
}

(async () => {
  const { api, showMsg } = VMS;
  try {
    const s = await api("/api/auth/setup", { noRedirect: true });
    if (s && s.needed) { location.href = "/setup"; return; }
  } catch (e) { /* sin conexión: se intenta entrar igual */ }
  const form = document.getElementById("form");
  const msg = document.getElementById("msg");
  form.querySelector("button").disabled = false;   // llega desactivado: ver login.html
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = form.querySelector("button");
    btn.disabled = true;
    try {
      await api("/api/auth/login", { method: "POST", noRedirect: true,
        json: { username: form.username.value.trim(), password: form.password.value } });
      location.href = safeNext(new URLSearchParams(location.search).get("next"));
    } catch (e) {
      showMsg(msg, e.message);
      form.password.value = "";
      form.password.focus();
    } finally {
      btn.disabled = false;
    }
  });
})();
