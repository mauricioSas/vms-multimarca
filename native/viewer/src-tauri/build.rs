//! Declara los comandos IPC del visor como permisos de la aplicación. Con un manifiesto propio, Tauri exige
//! que cada comando esté permitido en una capacidad (`capabilities/local.json`), y esa capacidad solo vale
//! para las páginas locales: las del backend (remotas) no pueden llamar a ninguno (PLAN-V2 §1.1).

/// Debe coincidir con `vms_viewer::app::COMMANDS` (lo comprueba una prueba).
const COMMANDS: &[&str] = &[
    "reintentar",
    "diagnostico",
    "servidores",
    "probar_servidor",
    "guardar_servidor",
    "borrar_servidor",
    "asignar_servidor_muro",
    "certificado",
    "confiar_certificado",
    "monitores",
    "asignar_monitor",
    "acerca",
    "actualizaciones",
    "buscar_actualizaciones",
    "volver_version_anterior",
    "sin_permiso",
];

fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("no se pudo preparar la compilación del visor");
}
