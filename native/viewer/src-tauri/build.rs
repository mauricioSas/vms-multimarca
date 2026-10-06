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
    "novedades",
    "instalar_novedad",
    "sin_permiso",
];

/// Manifiesto con Common Controls 6 para los ejecutables de PRUEBA en Windows. tauri-build solo lo incrusta en
/// `VMS.exe`; sin él, una prueba que enlaza Tauri entero (tests/ipc_acl.rs) no llega ni a arrancar
/// (STATUS_ENTRYPOINT_NOT_FOUND, comprobado en windows-latest).
const TEST_MANIFEST: &str = r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <dependency>
    <dependentAssembly>
      <assemblyIdentity type="win32" name="Microsoft.Windows.Common-Controls" version="6.0.0.0"
        processorArchitecture="*" publicKeyToken="6595b64144ccf1df" language="*"/>
    </dependentAssembly>
  </dependency>
</assembly>
"#;

fn main() {
    let windows_msvc = std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows")
        && std::env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("msvc");
    if windows_msvc {
        let out = std::path::PathBuf::from(std::env::var("OUT_DIR").expect("OUT_DIR"));
        let manifest = out.join("pruebas.manifest");
        std::fs::write(&manifest, TEST_MANIFEST).expect("manifiesto de pruebas");
        println!("cargo:rustc-link-arg-tests=/MANIFEST:EMBED");
        println!("cargo:rustc-link-arg-tests=/MANIFESTINPUT:{}", manifest.display());
    }
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("no se pudo preparar la compilación del visor");
}
