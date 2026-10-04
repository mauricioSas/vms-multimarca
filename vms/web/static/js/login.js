// Página de inicio de sesión: POST /api/auth/login y vuelta a la página pedida (?next=).
import { ApiError, get, post, safeNext } from "./api.js";
import { busy } from "./ui.js";

const form = document.getElementById("login-form");
const errorEl = document.getElementById("login-error");
const submit = document.getElementById("login-submit");
const requested = safeNext(new URLSearchParams(location.search).get("next") || "/");
const next = requested.startsWith("/login") || requested.startsWith("/setup") ? "/" : requested;

function showError(text) {
  errorEl.textContent = text;
  errorEl.hidden = !text;
}

async function checkSetup() {
  try {
    const res = await get("/api/auth/setup", { auth: false });
    document.getElementById("setup-banner").hidden = !(res && res.needed);
  } catch (err) {
    console.warn("No se pudo consultar el estado de configuración inicial", err);
  }
}

// El botón llega desactivado en el HTML: si se pulsara antes de cargar este script, el navegador
// enviaría el formulario por su cuenta (con la contraseña en la URL).
submit.disabled = false;
form.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  showError("");
  const username = form.username.value.trim();
  const password = form.password.value;
  if (!username || !password) {
    showError("Escribe el usuario y la contraseña.");
    (username ? form.password : form.username).focus();
    return;
  }
  await busy(submit, async () => {
    try {
      await post("/api/auth/login", { username, password }, { auth: false });
      location.replace(next);
    } catch (err) {
      if (err instanceof ApiError && err.status === 429) {
        const secs = err.details.retry_after;
        showError(secs ? `Demasiados intentos fallidos. Espera ${Math.ceil(secs / 60)} min y vuelve a probar.`
          : err.message);
      } else if (err instanceof ApiError && err.status === 401) {
        showError("Usuario o contraseña incorrectos.");
        form.password.select();
      } else {
        showError(err.message || "No se pudo iniciar sesión.");
      }
    }
  });
});

// si ya hay sesión, no hace falta volver a entrar
get("/api/auth/me", { auth: false }).then(() => location.replace(next)).catch(() => checkSetup());
