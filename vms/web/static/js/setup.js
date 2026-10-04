// Primer arranque: crea el administrador (POST /api/auth/setup, solo desde localhost).
import { ApiError, get, post } from "./api.js";
import { busy } from "./ui.js";

const form = document.getElementById("setup-form");
const errorEl = document.getElementById("setup-error");
const submit = document.getElementById("setup-submit");

function showError(text) {
  errorEl.textContent = text;
  errorEl.hidden = !text;
}

get("/api/auth/setup", { auth: false }).then((res) => {
  if (res && !res.needed) {
    document.getElementById("setup-done").hidden = false;
    form.hidden = true;
  }
}).catch((err) => console.warn("No se pudo consultar /api/auth/setup", err));

// El botón llega desactivado en el HTML: si se pulsara antes de cargar este script, el navegador
// enviaría el formulario por su cuenta (con la contraseña en la URL).
submit.disabled = false;
form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  showError("");
  const username = form.username.value.trim();
  const password = form.password.value;
  if (!/^[A-Za-z0-9._-]{3,32}$/.test(username)) {
    showError("El usuario debe tener de 3 a 32 caracteres: letras, números, punto, guion o guion bajo.");
    return;
  }
  if (password.length < 8) {
    showError("La contraseña debe tener al menos 8 caracteres.");
    return;
  }
  if (password !== form.password2.value) {
    showError("Las contraseñas no coinciden.");
    return;
  }
  await busy(submit, async () => {
    try {
      await post("/api/auth/setup", { username, password }, { auth: false });
      await post("/api/auth/login", { username, password }, { auth: false });
      location.replace("/");
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        showError("El administrador solo se puede crear desde el propio equipo del VMS (127.0.0.1).");
      } else if (err instanceof ApiError && err.status === 409) {
        showError("Ya existe un usuario. Inicia sesión.");
      } else {
        showError(err.message || "No se pudo crear el administrador.");
      }
    }
  });
});
