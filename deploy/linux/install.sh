#!/usr/bin/env bash
# =============================================================================
# VMS Multimarca — instalación en Linux (Ubuntu 24.04 LTS recomendado; Debian con Python 3.12)
#
# Uso (como root, desde la carpeta del paquete):
#   sudo deploy/linux/install.sh [opciones]
#
# Opciones:
#   --components LISTA   separados por comas: backend,analytics,heartbeat,central
#                        (por defecto: backend,analytics,heartbeat = mini PC de tienda)
#   --site-id ID         identificador de la sede (p. ej. site-bcn-001)
#   --central-url URL    URL del panel central (p. ej. https://central.vpn:8700)
#   --prefix RUTA        carpeta de instalación (por defecto /opt/vms-multimarca)
#   --wheelhouse RUTA    wheels ya descargadas (instalación sin Internet)
#   --downloads RUTA     carpeta con mediamtx_*.tar.gz ya descargado (sin Internet)
#   --no-firewall        no tocar ufw
#   --no-start           instalar sin arrancar los servicios
#   -h, --help           esta ayuda
#
# Es idempotente: se puede volver a ejecutar para actualizar. Nunca pisa /etc/vms-multimarca/vms.env.
# =============================================================================
set -Eeuo pipefail

MEDIAMTX_VERSION="v1.21.1"
MEDIAMTX_SHA256_AMD64="653abc672a3e693f8d3b2717752492fdcfb8072291ec108d03d3dd857411b0ee"
MEDIAMTX_SHA256_ARM64="6a3aa635fb60ea9b8d566ec306f0a42ff1b6b52a3942bc2baffbe55880d4c3dd"

PREFIX="/opt/vms-multimarca"
DATA_DIR="/var/lib/vms-multimarca"
ETC_DIR="/etc/vms-multimarca"
SERVICE_USER="vms"
COMPONENTS="backend,analytics,heartbeat"
SITE_ID=""
CENTRAL_URL=""
WHEELHOUSE=""
DOWNLOADS=""
DO_FIREWALL=1
DO_START=1
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# Redes privadas desde las que se permite entrar (LAN de la tienda y VPN Tailscale/Headscale)
PRIVATE_NETS=("10.0.0.0/8" "172.16.0.0/12" "192.168.0.0/16" "100.64.0.0/10")

step() { printf '\n==> %s\n' "$*"; }
ok()   { printf '    OK  %s\n' "$*"; }
warn() { printf '    AVISO  %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
trap 'die "falló la línea ${LINENO}: ${BASH_COMMAND}"' ERR

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --components) COMPONENTS="${2:?falta el valor}"; shift 2 ;;
        --site-id) SITE_ID="${2:?falta el valor}"; shift 2 ;;
        --central-url) CENTRAL_URL="${2:?falta el valor}"; shift 2 ;;
        --prefix) PREFIX="${2:?falta el valor}"; shift 2 ;;
        --wheelhouse) WHEELHOUSE="${2:?falta el valor}"; shift 2 ;;
        --downloads) DOWNLOADS="${2:?falta el valor}"; shift 2 ;;
        --no-firewall) DO_FIREWALL=0; shift ;;
        --no-start) DO_START=0; shift ;;
        -h|--help) usage ;;
        *) die "opción desconocida: $1 (usa --help)" ;;
    esac
done

has() { [[ ",${COMPONENTS}," == *",$1,"* ]]; }
for c in ${COMPONENTS//,/ }; do
    case "$c" in backend|analytics|heartbeat|central) ;; *) die "componente desconocido: $c" ;; esac
done
if [[ -n "$SITE_ID" && ! "$SITE_ID" =~ ^[a-z0-9][a-z0-9-]{2,39}$ ]]; then
    die "--site-id no válido (minúsculas, números y guiones; 3 a 40 caracteres)"
fi
if [[ -n "$CENTRAL_URL" && ! "$CENTRAL_URL" =~ ^https?:// ]]; then
    die "--central-url debe empezar por http:// o https://"
fi

# --------------------------------------------------------------------------- 1. comprobaciones
step "Comprobando el sistema"
[[ $EUID -eq 0 ]] || die "ejecuta este script como root (sudo)"
command -v systemctl >/dev/null || die "se necesita systemd"
[[ -f "${SOURCE_DIR}/vms/__init__.py" ]] || die "no encuentro la aplicación en ${SOURCE_DIR}"
case "$(uname -m)" in
    x86_64|amd64) MTX_ARCH="linux_amd64"; MTX_SHA256="$MEDIAMTX_SHA256_AMD64" ;;
    aarch64|arm64) MTX_ARCH="linux_arm64"; MTX_SHA256="$MEDIAMTX_SHA256_ARM64" ;;
    *) die "arquitectura no soportada: $(uname -m)" ;;
esac
# shellcheck source=/dev/null
ok "$(. /etc/os-release && echo "${PRETTY_NAME:-Linux}") · $(uname -m) · componentes: ${COMPONENTS}"

PYTHON="$(command -v python3.12 || true)"
if [[ -z "$PYTHON" ]] && command -v apt-get >/dev/null; then
    step "Instalando Python 3.12 desde los repositorios del sistema"
    apt-get update -qq
    if apt-get install -y -qq python3.12 python3.12-venv >/dev/null; then
        PYTHON="$(command -v python3.12)"
    fi
fi
[[ -n "$PYTHON" ]] || die "se necesita Python 3.12 (Ubuntu 24.04 LTS lo trae de serie). Ver docs/INSTALACION-LINUX.md"
"$PYTHON" -c 'import venv, ensurepip' 2>/dev/null || {
    command -v apt-get >/dev/null && apt-get install -y -qq python3.12-venv >/dev/null
    "$PYTHON" -c 'import venv, ensurepip' 2>/dev/null || die "falta el módulo venv (paquete python3.12-venv)"
}
for tool in curl tar sha256sum; do command -v "$tool" >/dev/null || die "falta la herramienta $tool"; done
ok "Python: $("$PYTHON" --version)"

# --------------------------------------------------------------------------- 2. usuario y carpetas
step "Usuario de servicio y carpetas"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "usuario ${SERVICE_USER} creado"
fi
for g in video render; do
    if getent group "$g" >/dev/null; then usermod -aG "$g" "$SERVICE_USER"; fi   # aceleración Intel (OpenVINO GPU)
done
install -d -m 0755 -o root -g root "$PREFIX"
install -d -m 0750 -o root -g "$SERVICE_USER" "$ETC_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR"
ok "${PREFIX} · ${ETC_DIR} · ${DATA_DIR}"

# --------------------------------------------------------------------------- 3. copia de la aplicación
step "Copiando la aplicación a ${PREFIX}"
if [[ "$(cd "$PREFIX" && pwd)" != "$SOURCE_DIR" ]]; then
    for d in vms analytics central deploy; do
        rm -rf "${PREFIX:?}/${d}"
        cp -a "${SOURCE_DIR}/${d}" "${PREFIX}/${d}"
    done
    if [[ -d "${SOURCE_DIR}/models" ]] && has analytics; then
        rm -rf "${PREFIX:?}/models"
        cp -a "${SOURCE_DIR}/models" "${PREFIX}/models"
        # Los checkpoints .pth (750 MB) solo sirven para exportar: no se distribuyen.
        rm -rf "${PREFIX:?}/models/weights"
        find "${PREFIX}/models" -maxdepth 1 \( -name '*.pth' -o -name '*.pt' \) -delete
    fi
    for f in pyproject.toml .env.example LEEME.md THIRD_PARTY_NOTICES.txt requirements-vms.txt \
             requirements-analytics.txt requirements-central.txt; do
        if [[ -f "${SOURCE_DIR}/${f}" ]]; then install -m 0644 "${SOURCE_DIR}/${f}" "${PREFIX}/${f}"; fi
    done
    find "$PREFIX" -name '__pycache__' -type d -prune -exec rm -rf {} +
    chown -R root:root "$PREFIX"
fi
ok "aplicación copiada"

# --------------------------------------------------------------------------- 4. entorno Python
step "Entorno de Python y dependencias (archivos de bloqueo, --no-deps)"
if [[ ! -x "${PREFIX}/.venv/bin/python" ]]; then
    "$PYTHON" -m venv "${PREFIX}/.venv"
fi
SITE_PACKAGES="$("${PREFIX}/.venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
printf '%s\n' "$PREFIX" > "${SITE_PACKAGES}/vms-multimarca.pth"
REQS=()
has backend && REQS+=("requirements-vms.txt")
has analytics && REQS+=("requirements-analytics.txt")
{ has heartbeat || has central; } && REQS+=("requirements-central.txt")
PIP_ARGS=(install --no-deps --require-hashes --only-binary=:all: --disable-pip-version-check -q)
if [[ -n "$WHEELHOUSE" ]]; then PIP_ARGS+=(--no-index --find-links "$WHEELHOUSE"); fi
for req in "${REQS[@]}"; do
    "${PREFIX}/.venv/bin/python" -m pip "${PIP_ARGS[@]}" -r "${PREFIX}/${req}"
    ok "$req"
done
"${PREFIX}/.venv/bin/python" -c 'import vms, central; print("    OK  VMS Multimarca", vms.__version__)'

# --------------------------------------------------------------------------- 5. MediaMTX
if has backend; then
    step "MediaMTX ${MEDIAMTX_VERSION} (${MTX_ARCH})"
    archive="mediamtx_${MEDIAMTX_VERSION}_${MTX_ARCH}.tar.gz"
    tmp="$(mktemp -d)"
    if [[ -n "$DOWNLOADS" && -f "${DOWNLOADS}/${archive}" ]]; then
        cp "${DOWNLOADS}/${archive}" "${tmp}/${archive}"
    else
        curl -fsSL --retry 3 -o "${tmp}/${archive}" \
            "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/${archive}"
    fi
    echo "${MTX_SHA256}  ${tmp}/${archive}" | sha256sum -c --quiet - \
        || { rm -rf "$tmp"; die "SHA-256 incorrecto en ${archive}: descarga cancelada por seguridad"; }
    tar -xzf "${tmp}/${archive}" -C "$tmp" mediamtx LICENSE
    install -d -m 0755 "${PREFIX}/bin"
    install -m 0755 "${tmp}/mediamtx" "${PREFIX}/bin/mediamtx"
    install -m 0644 "${tmp}/LICENSE" "${PREFIX}/bin/MEDIAMTX-LICENSE.txt"
    rm -rf "$tmp"
    ok "MediaMTX verificado en ${PREFIX}/bin"
fi

# --------------------------------------------------------------------------- 6. configuración
step "Configuración ${ETC_DIR}/vms.env"
ENV_FILE="${ETC_DIR}/vms.env"
set_env() {   # set_env CLAVE VALOR [solo_si_vacio]
    local key="$1" value="$2" only_empty="${3:-}"
    if grep -qE "^${key}=" "$ENV_FILE"; then
        local current
        current="$(grep -E "^${key}=" "$ENV_FILE" | tail -n1 | cut -d= -f2-)"
        if [[ -n "$only_empty" && -n "$current" ]]; then return 0; fi
        local tmpf
        tmpf="$(mktemp)"
        awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' "$ENV_FILE" > "$tmpf"
        cat "$tmpf" > "$ENV_FILE"
        rm -f "$tmpf"
    else
        printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}
if [[ ! -f "$ENV_FILE" ]]; then
    install -m 0640 -o root -g "$SERVICE_USER" "${PREFIX}/.env.example" "$ENV_FILE"
    ok "vms.env creado desde .env.example"
else
    ok "vms.env ya existía: se conserva (solo se completan valores vacíos)"
fi
set_env VMS_DATA_DIR "$DATA_DIR"
set_env VMS_CREDENTIAL_BACKEND file
set_env VMS_KIOSK_TOKEN "$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))')" only_empty
[[ -n "$SITE_ID" ]] && set_env VMS_SITE_ID "$SITE_ID"
[[ -n "$CENTRAL_URL" ]] && set_env VMS_CENTRAL_URL "$CENTRAL_URL"
if has heartbeat; then set_env VMS_CENTRAL_URL "" only_empty; set_env VMS_SITE_TOKEN "" only_empty; fi
if has central; then set_env VMS_CENTRAL_DATA_DIR "${DATA_DIR}/central" only_empty; fi
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"
ok "permisos 0640 root:${SERVICE_USER}"
env_value() { grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2- || true; }

# --------------------------------------------------------------------------- 7. servicios
step "Servicios systemd"
UNITS=()
has backend && UNITS+=("vms")
has analytics && UNITS+=("vms-analytics")
has heartbeat && UNITS+=("vms-heartbeat")
has central && UNITS+=("vms-central")
UNIT_FILES=()
for u in "${UNITS[@]}"; do UNIT_FILES+=("${u}.service"); done
has central && UNIT_FILES+=("vms-central-reports.service" "vms-central-reports.timer")
for f in "${UNIT_FILES[@]}"; do
    sed "s#/opt/vms-multimarca#${PREFIX}#g" "${SCRIPT_DIR}/systemd/${f}" > "/etc/systemd/system/${f}"
    chmod 0644 "/etc/systemd/system/${f}"
done
systemctl daemon-reload
for u in "${UNITS[@]}"; do
    systemctl enable "${u}.service" >/dev/null 2>&1
    ok "${u}.service habilitado"
done
if has central; then
    systemctl enable vms-central-reports.timer >/dev/null 2>&1
    ok "vms-central-reports.timer habilitado (lunes 06:00 Europe/Madrid)"
fi

# --------------------------------------------------------------------------- 8. firewall (ufw)
if [[ $DO_FIREWALL -eq 1 ]]; then
    step "Firewall (solo redes privadas y VPN)"
    if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
        RULES=()
        has backend && RULES+=("8600/tcp" "8189/udp" "8189/tcp")
        has central && RULES+=("8700/tcp")
        for r in "${RULES[@]}"; do
            for net in "${PRIVATE_NETS[@]}"; do
                ufw allow from "$net" to any port "${r%/*}" proto "${r#*/}" comment 'vms-multimarca' >/dev/null
            done
            ok "${r} desde ${PRIVATE_NETS[*]}"
        done
    else
        warn "ufw no está activo: abre a mano 8600/tcp y 8189/udp+tcp SOLO a la red de la tienda/VPN (ver docs/RED.md)"
    fi
fi

# --------------------------------------------------------------------------- 9. arranque
if [[ $DO_START -eq 1 ]]; then
    step "Arrancando"
    for u in "${UNITS[@]}"; do
        if [[ "$u" == "vms-heartbeat" ]] && { [[ -z "$(env_value VMS_CENTRAL_URL)" ]] || [[ -z "$(env_value VMS_SITE_TOKEN)" ]]; }; then
            warn "${u} no se arranca: completa VMS_CENTRAL_URL y VMS_SITE_TOKEN en ${ENV_FILE} y luego: systemctl start ${u}"
            continue
        fi
        if [[ "$u" == "vms-central" ]] && [[ -z "$(env_value VMS_PG_DSN)" ]] && [[ -z "$(env_value VMS_CENTRAL_PG_DSN)" ]]; then
            warn "${u} no se arranca: completa VMS_PG_DSN en ${ENV_FILE} y luego: systemctl start ${u}"
            continue
        fi
        systemctl restart "${u}.service"
        ok "${u} en marcha"
        if [[ "$u" == "vms-central" ]]; then
            systemctl start vms-central-reports.timer
            ok "informe semanal programado: systemctl list-timers vms-central-reports.timer"
        fi
    done
    if has backend; then
        port="$(env_value VMS_HTTP_PORT)"; port="${port:-8600}"
        for _ in $(seq 1 30); do
            if curl -fsS "http://127.0.0.1:${port}/api/health" >/dev/null 2>&1; then
                ok "backend responde: $(curl -fsS "http://127.0.0.1:${port}/api/health")"
                break
            fi
            sleep 2
        done || true
        curl -fsS "http://127.0.0.1:${port}/api/health" >/dev/null 2>&1 \
            || warn "el backend no responde todavía: journalctl -u vms -n 100"
    fi
fi

if has analytics; then
    step "Diagnóstico de la analítica (python -m analytics check)"
    if ! compgen -G "${PREFIX}/models/rfdetr-*" >/dev/null; then
        warn "no hay modelo de detección en ${PREFIX}/models: copia allí los archivos rfdetr-nano.* exportados"
    fi
    if (cd "$PREFIX" && runuser -u "$SERVICE_USER" -- env VMS_DATA_DIR="$DATA_DIR" VMS_ENV_FILE="$ENV_FILE" \
        "${PREFIX}/.venv/bin/python" -m analytics check); then
        ok "analítica lista"
    else
        warn "el diagnóstico de la analítica indica problemas (revisa los mensajes de arriba)"
    fi
fi

printf '\nInstalación terminada.\n'
printf '  Configuración: %s\n  Datos y registros: %s\n  Estado: systemctl status %s\n' \
    "$ENV_FILE" "$DATA_DIR" "${UNITS[*]}"
