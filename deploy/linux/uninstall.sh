#!/usr/bin/env bash
# =============================================================================
# VMS Multimarca — desinstalación en Linux
#
# Uso:  sudo deploy/linux/uninstall.sh [--prefix /opt/vms-multimarca] [--purge] [--yes]
#
#   Sin --purge: quita servicios, reglas de ufw y la carpeta de instalación, pero CONSERVA
#   la configuración (/etc/vms-multimarca) y los datos y grabaciones (/var/lib/vms-multimarca).
#   Con --purge: borra también configuración, contraseñas cifradas, registros y GRABACIONES
#   (pide confirmación salvo con --yes). No se puede deshacer.
# =============================================================================
set -Eeuo pipefail

PREFIX="/opt/vms-multimarca"
DATA_DIR="/var/lib/vms-multimarca"
ETC_DIR="/etc/vms-multimarca"
SERVICE_USER="vms"
PURGE=0
ASSUME_YES=0
PRIVATE_NETS=("10.0.0.0/8" "172.16.0.0/12" "192.168.0.0/16" "100.64.0.0/10")

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix) PREFIX="${2:?falta el valor}"; shift 2 ;;
        --purge) PURGE=1; shift ;;
        --yes) ASSUME_YES=1; shift ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Opción desconocida: $1" >&2; exit 2 ;;
    esac
done
[[ $EUID -eq 0 ]] || { echo "Ejecuta este script como root (sudo)" >&2; exit 1; }

echo "==> Servicios"
for f in vms-central-reports.timer vms-central-reports.service vms-analytics.service vms-heartbeat.service \
         vms-central.service vms.service; do
    if [[ -f "/etc/systemd/system/${f}" ]]; then
        systemctl disable --now "${f}" >/dev/null 2>&1 || true
        rm -f "/etc/systemd/system/${f}"
        echo "    OK  ${f} eliminado"
    fi
done
systemctl daemon-reload

echo "==> Firewall"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    for r in 8600/tcp 8189/udp 8189/tcp 8700/tcp; do
        for net in "${PRIVATE_NETS[@]}"; do
            ufw delete allow from "$net" to any port "${r%/*}" proto "${r#*/}" >/dev/null 2>&1 || true
        done
    done
    echo "    OK  reglas de vms-multimarca eliminadas"
fi

echo "==> Carpeta de instalación ${PREFIX}"
if [[ -d "$PREFIX" && -f "${PREFIX}/vms/__init__.py" ]]; then
    rm -rf "${PREFIX:?}"
    echo "    OK  eliminada"
fi

if [[ $PURGE -eq 1 ]]; then
    if [[ $ASSUME_YES -ne 1 ]]; then
        read -r -p "Se borrarán ${ETC_DIR} y ${DATA_DIR}, incluidas TODAS LAS GRABACIONES. Escribe BORRAR para confirmar: " answer
        [[ "$answer" == "BORRAR" ]] || { echo "Cancelado: los datos se conservan."; exit 0; }
    fi
    rm -rf "${ETC_DIR:?}" "${DATA_DIR:?}"
    if id "$SERVICE_USER" >/dev/null 2>&1; then userdel "$SERVICE_USER" || true; fi
    echo "    OK  configuración, datos y usuario ${SERVICE_USER} eliminados"
else
    echo
    echo "Se conservan la configuración (${ETC_DIR}) y los datos y grabaciones (${DATA_DIR})."
fi
echo "Desinstalación terminada."
