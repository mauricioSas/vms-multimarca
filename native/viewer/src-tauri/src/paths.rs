//! Dónde están los datos, la configuración del visor y la instalación (CONTRATO §13.1 y §17).
//!
//! - Datos (`%ProgramData%\VMSMultimarca`, o `VMS_DATA_DIR` como el backend): `secrets\kiosk.token` y
//!   `updater\public-status.json`.
//! - Configuración del visor, por usuario: `%APPDATA%\VMSMultimarca\viewer.json`.
//! - Instalación: el visor vive en `<raíz>\versions\<X.Y.Z>\viewer\VMS.exe`; de ahí salen `vmsctl.exe`
//!   (`versions\<X.Y.Z>\bin\`), `vmshost.exe` (`<raíz>\bin\`) y los avisos de terceros.

use std::env;
use std::path::{Path, PathBuf};

pub const APP_ID: &str = "VMSMultimarca";
const LINUX_DIR_NAME: &str = "vms-multimarca";

fn env_path(name: &str) -> Option<PathBuf> {
    env::var_os(name).filter(|v| !v.is_empty()).map(PathBuf::from)
}

fn home() -> PathBuf {
    env_path("HOME").or_else(|| env_path("USERPROFILE")).unwrap_or_else(|| PathBuf::from("."))
}

/// Carpeta de datos compartida con los servicios (misma regla que `vms.core.paths.default_data_dir`).
pub fn data_dir() -> PathBuf {
    if let Some(p) = env_path("VMS_DATA_DIR") {
        return p;
    }
    if cfg!(windows) {
        let base = env_path("ProgramData")
            .or_else(|| env_path("PROGRAMDATA"))
            .or_else(|| env_path("ALLUSERSPROFILE"))
            .unwrap_or_else(|| PathBuf::from(r"C:\ProgramData"));
        return base.join(APP_ID);
    }
    if cfg!(target_os = "macos") {
        return home().join("Library").join("Application Support").join(APP_ID);
    }
    env_path("XDG_DATA_HOME").unwrap_or_else(|| home().join(".local").join("share")).join(LINUX_DIR_NAME)
}

/// Carpeta de configuración del visor, por usuario (`%APPDATA%\VMSMultimarca`). `VMS_VIEWER_CONFIG_DIR` la
/// cambia (pruebas y laboratorio; solo afecta a la configuración del propio usuario).
pub fn config_dir() -> PathBuf {
    if let Some(p) = env_path("VMS_VIEWER_CONFIG_DIR") {
        return p;
    }
    if cfg!(windows) {
        if let Some(p) = env_path("APPDATA") {
            return p.join(APP_ID);
        }
        return home().join("AppData").join("Roaming").join(APP_ID);
    }
    if cfg!(target_os = "macos") {
        return home().join("Library").join("Application Support").join(APP_ID).join("visor");
    }
    env_path("XDG_CONFIG_HOME").unwrap_or_else(|| home().join(".config")).join(LINUX_DIR_NAME)
}

pub fn viewer_config_file() -> PathBuf {
    config_dir().join("viewer.json")
}

pub fn kiosk_token_file(data: &Path) -> PathBuf {
    data.join("secrets").join("kiosk.token")
}

pub fn public_status_file(data: &Path) -> PathBuf {
    data.join("updater").join("public-status.json")
}

/// Registro del visor (por usuario, junto a `viewer.json`).
pub fn log_file() -> PathBuf {
    config_dir().join("visor.log")
}

/// Versión válida como nombre de carpeta: sin separadores ni `..` (viene de un JSON del disco).
pub fn safe_version(v: &str) -> bool {
    !v.is_empty()
        && v.len() <= 40
        && !v.starts_with('.')
        && v.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | '+' | '_'))
        && !v.contains("..")
}

/// Disposición de una instalación de la v2 deducida de la ruta del propio ejecutable.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InstallLayout {
    pub root: PathBuf,
    pub version: String,
}

impl InstallLayout {
    /// `<raíz>\versions\<X.Y.Z>\viewer\VMS.exe` → `{root, version}`; `None` en desarrollo.
    pub fn from_exe(exe: &Path) -> Option<Self> {
        let viewer_dir = exe.parent()?;
        if !viewer_dir.file_name()?.to_string_lossy().eq_ignore_ascii_case("viewer") {
            return None;
        }
        let version_dir = viewer_dir.parent()?;
        let version = version_dir.file_name()?.to_string_lossy().into_owned();
        let versions = version_dir.parent()?;
        if !versions.file_name()?.to_string_lossy().eq_ignore_ascii_case("versions") || !safe_version(&version) {
            return None;
        }
        Some(Self { root: versions.parent()?.to_path_buf(), version })
    }

    pub fn version_dir(&self, version: &str) -> Option<PathBuf> {
        safe_version(version).then(|| self.root.join("versions").join(version))
    }

    pub fn viewer_exe(&self, version: &str) -> Option<PathBuf> {
        Some(self.version_dir(version)?.join("viewer").join(exe_name("VMS")))
    }

    pub fn vmsctl(&self) -> PathBuf {
        self.root.join("versions").join(&self.version).join("bin").join(exe_name("vmsctl"))
    }

    pub fn vmshost(&self) -> PathBuf {
        self.root.join("bin").join(exe_name("vmshost"))
    }

    pub fn notices(&self) -> PathBuf {
        self.root.join("versions").join(&self.version).join("THIRD_PARTY_NOTICES.txt")
    }
}

fn exe_name(stem: &str) -> String {
    if cfg!(windows) {
        format!("{stem}.exe")
    } else {
        stem.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn layout_from_versioned_exe() {
        let exe = Path::new("/pf/VMSMultimarca/versions/2.1.0/viewer/VMS.exe");
        let l = InstallLayout::from_exe(exe).expect("disposición");
        assert_eq!(l.root, Path::new("/pf/VMSMultimarca"));
        assert_eq!(l.version, "2.1.0");
        assert!(l.vmsctl().starts_with("/pf/VMSMultimarca/versions/2.1.0/bin"));
        assert!(l.vmshost().starts_with("/pf/VMSMultimarca/bin"));
        let next = l.viewer_exe("2.1.1").unwrap();
        assert!(next.starts_with("/pf/VMSMultimarca/versions/2.1.1/viewer"));
    }

    #[test]
    fn layout_absent_in_development() {
        assert_eq!(InstallLayout::from_exe(Path::new("/repo/native/viewer/src-tauri/target/debug/VMS")), None);
        assert_eq!(InstallLayout::from_exe(Path::new("VMS.exe")), None);
    }

    #[test]
    fn versions_from_disk_cannot_escape() {
        assert!(safe_version("2.1.0"));
        assert!(safe_version("2.0.0-dev.0"));
        for bad in ["", "..", "../x", "2.0/../../x", "a\\b", ".hidden", "c:x", "2.1.0 x"] {
            assert!(!safe_version(bad), "{bad}");
        }
        let l = InstallLayout { root: PathBuf::from("/r"), version: "2.0.0".into() };
        assert_eq!(l.viewer_exe("../../evil"), None);
    }

    #[test]
    fn data_files_are_where_the_contract_says() {
        let d = Path::new("/datos");
        assert_eq!(kiosk_token_file(d), Path::new("/datos/secrets/kiosk.token"));
        assert_eq!(public_status_file(d), Path::new("/datos/updater/public-status.json"));
    }
}
