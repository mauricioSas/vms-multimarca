//! Disposición en disco (CONTRATO §13.1): carpeta de instalación y carpeta de datos.
//!
//! ```text
//! C:\Program Files\VMSMultimarca\           InstallLayout
//! ├── bin\vmshost.exe
//! ├── versions\<X.Y.Z>\bin\vmsctl.exe, runtime\, app\, engine\, viewer\, models\
//! └── updater\slot-a\ y slot-b\            vmsctl.exe + runtime\ + app\ (vms_updater)
//! C:\ProgramData\VMSMultimarca\             DataLayout (igual que vms.core.paths.AppPaths)
//! ```

use std::path::{Path, PathBuf};

pub const APP_ID: &str = "VMSMultimarca";
const LINUX_DIR_NAME: &str = "vms-multimarca";

/// `nombre.exe` en Windows, `nombre` en el resto.
pub fn exe_name(name: &str) -> String {
    if cfg!(windows) {
        format!("{name}.exe")
    } else {
        name.to_string()
    }
}

/// Carpeta de datos por defecto, la misma que `vms.core.paths.default_data_dir` (sin `VMS_DATA_DIR`).
pub fn default_data_dir() -> PathBuf {
    if cfg!(windows) {
        let base = std::env::var_os("PROGRAMDATA")
            .or_else(|| std::env::var_os("ALLUSERSPROFILE"))
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(r"C:\ProgramData"));
        return base.join(APP_ID);
    }
    let home = std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("."));
    if cfg!(target_os = "macos") {
        return home.join("Library").join("Application Support").join(APP_ID);
    }
    match std::env::var_os("XDG_DATA_HOME") {
        Some(x) if !x.is_empty() => PathBuf::from(x).join(LINUX_DIR_NAME),
        _ => home.join(".local").join("share").join(LINUX_DIR_NAME),
    }
}

/// `explícita` → `VMS_DATA_DIR` → por defecto.
pub fn resolve_data_dir(explicit: Option<&Path>) -> PathBuf {
    if let Some(p) = explicit {
        return p.to_path_buf();
    }
    match std::env::var_os("VMS_DATA_DIR") {
        Some(v) if !v.is_empty() => PathBuf::from(v),
        _ => default_data_dir(),
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InstallLayout {
    pub root: PathBuf,
}

/// Dónde vive el `vmsctl` que se está ejecutando.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Component {
    /// `versions\<X>\bin\vmsctl.exe`
    Version { version: String, root: PathBuf },
    /// `updater\slot-<x>\vmsctl.exe`
    UpdaterSlot { slot: String, root: PathBuf },
}

impl Component {
    /// Carpeta que contiene `runtime\`, `app\`, `engine\`…
    pub fn root(&self) -> &Path {
        match self {
            Component::Version { root, .. } | Component::UpdaterSlot { root, .. } => root,
        }
    }
}

impl InstallLayout {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }
    pub fn bin_dir(&self) -> PathBuf {
        self.root.join("bin")
    }
    pub fn vmshost_exe(&self) -> PathBuf {
        self.bin_dir().join(exe_name("vmshost"))
    }
    pub fn versions_dir(&self) -> PathBuf {
        self.root.join("versions")
    }
    pub fn version_dir(&self, v: &str) -> PathBuf {
        self.versions_dir().join(v)
    }
    pub fn version_vmsctl(&self, v: &str) -> PathBuf {
        self.version_dir(v).join("bin").join(exe_name("vmsctl"))
    }
    pub fn version_installed(&self, v: &str) -> bool {
        crate::state::validate_version(v).is_ok() && self.version_vmsctl(v).is_file()
    }
    pub fn slot_dir(&self, slot: &str) -> PathBuf {
        self.root.join("updater").join(format!("slot-{slot}"))
    }
    pub fn slot_vmsctl(&self, slot: &str) -> PathBuf {
        self.slot_dir(slot).join(exe_name("vmsctl"))
    }

    /// Versiones instaladas (carpetas de `versions\` con `bin\vmsctl`), ordenadas por nombre.
    pub fn installed_versions(&self) -> Vec<String> {
        let mut out: Vec<String> = std::fs::read_dir(self.versions_dir())
            .map(|rd| {
                rd.filter_map(Result::ok)
                    .filter_map(|e| e.file_name().to_str().map(str::to_string))
                    .filter(|v| self.version_installed(v))
                    .collect()
            })
            .unwrap_or_default();
        out.sort();
        out
    }

    /// Deduce la instalación a partir de la ruta de `vmsctl` (`versions\X\bin\vmsctl.exe` o
    /// `updater\slot-x\vmsctl.exe`). `None` si el ejecutable no está en ninguno de los dos sitios.
    pub fn from_vmsctl_exe(exe: &Path) -> Option<(InstallLayout, Component)> {
        let dir = exe.parent()?;
        let name = |p: &Path| p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        if name(dir).eq_ignore_ascii_case("bin") {
            let vdir = dir.parent()?;
            let versions = vdir.parent()?;
            if name(versions).eq_ignore_ascii_case("versions") {
                let version = name(vdir);
                crate::state::validate_version(&version).ok()?;
                let install = InstallLayout::new(versions.parent()?);
                return Some((install, Component::Version { version, root: vdir.to_path_buf() }));
            }
        }
        let slot_name = name(dir);
        let updater = dir.parent()?;
        if let Some(slot) = slot_name.strip_prefix("slot-") {
            if name(updater).eq_ignore_ascii_case("updater") && crate::state::validate_slot(slot).is_ok() {
                let install = InstallLayout::new(updater.parent()?);
                return Some((install, Component::UpdaterSlot { slot: slot.to_string(), root: dir.to_path_buf() }));
            }
        }
        None
    }
}

/// Carpeta de datos (`%ProgramData%\VMSMultimarca`): mismos nombres que `vms.core.paths.AppPaths`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DataLayout {
    pub root: PathBuf,
}

impl DataLayout {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self { root: root.into() }
    }
    pub fn state_dir(&self) -> PathBuf {
        self.root.join("state")
    }
    pub fn state(&self) -> crate::state::StateDir {
        crate::state::StateDir::new(self.state_dir())
    }
    pub fn logs_dir(&self) -> PathBuf {
        self.root.join("logs")
    }
    pub fn config_dir(&self) -> PathBuf {
        self.root.join("config")
    }
    pub fn secrets_dir(&self) -> PathBuf {
        self.root.join("secrets")
    }
    pub fn recordings_dir(&self) -> PathBuf {
        self.root.join("recordings")
    }
    pub fn mediamtx_dir(&self) -> PathBuf {
        self.root.join("mediamtx")
    }
    pub fn mediamtx_yml(&self) -> PathBuf {
        self.mediamtx_dir().join("mediamtx.yml")
    }
    pub fn analytics_dir(&self) -> PathBuf {
        self.root.join("analytics")
    }
    pub fn central_dir(&self) -> PathBuf {
        self.root.join("central")
    }
    pub fn updater_dir(&self) -> PathBuf {
        self.root.join("updater")
    }
    pub fn backups_dir(&self) -> PathBuf {
        self.root.join("backups")
    }
    pub fn ops_dir(&self) -> PathBuf {
        self.root.join("ops")
    }
    pub fn evidence_dir(&self) -> PathBuf {
        self.root.join("evidence")
    }
    pub fn env_file(&self) -> PathBuf {
        self.root.join(".env")
    }

    /// Carpetas que crea el instalador (las que faltan; no toca las que ya existen).
    pub fn all_dirs(&self) -> Vec<PathBuf> {
        vec![
            self.root.clone(),
            self.config_dir(),
            self.secrets_dir(),
            self.logs_dir(),
            self.recordings_dir(),
            self.mediamtx_dir(),
            self.analytics_dir(),
            self.state_dir(),
            self.state().requests_dir(),
            self.updater_dir(),
            self.backups_dir(),
            self.ops_dir(),
            self.evidence_dir(),
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn component_from_version_and_slot_paths() {
        let root = Path::new("/pf/VMSMultimarca");
        let exe = root.join("versions").join("2.1.0").join("bin").join(exe_name("vmsctl"));
        let (inst, comp) = InstallLayout::from_vmsctl_exe(&exe).unwrap();
        assert_eq!(inst.root, root);
        assert_eq!(comp, Component::Version { version: "2.1.0".into(), root: root.join("versions").join("2.1.0") });
        assert_eq!(inst.version_vmsctl("2.1.0"), exe);

        let exe = root.join("updater").join("slot-b").join(exe_name("vmsctl"));
        let (inst, comp) = InstallLayout::from_vmsctl_exe(&exe).unwrap();
        assert_eq!(inst.root, root);
        assert_eq!(comp.root(), root.join("updater").join("slot-b"));
        assert_eq!(inst.slot_vmsctl("b"), exe);

        assert!(InstallLayout::from_vmsctl_exe(Path::new("/usr/local/bin/vmsctl")).is_none());
        assert!(InstallLayout::from_vmsctl_exe(&root.join("updater").join("slot-z").join("vmsctl")).is_none());
    }

    #[test]
    fn installed_versions_need_vmsctl() {
        let d = tempfile::tempdir().unwrap();
        let inst = InstallLayout::new(d.path());
        for v in ["2.0.0", "2.1.0"] {
            std::fs::create_dir_all(inst.version_dir(v).join("bin")).unwrap();
            std::fs::write(inst.version_vmsctl(v), b"").unwrap();
        }
        std::fs::create_dir_all(inst.version_dir("2.2.0.tmp")).unwrap(); // a medio descomprimir
        assert_eq!(inst.installed_versions(), ["2.0.0", "2.1.0"]);
        assert!(!inst.version_installed("../2.0.0"));
    }

    #[test]
    fn data_layout_matches_python_names() {
        let d = DataLayout::new("/data");
        assert_eq!(d.mediamtx_yml(), Path::new("/data/mediamtx/mediamtx.yml"));
        assert_eq!(d.state().pointer_path(), Path::new("/data/state/active.json"));
        assert!(d.all_dirs().contains(&PathBuf::from("/data/state/requests")));
    }
}
