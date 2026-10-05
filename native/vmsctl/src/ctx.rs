//! Dónde está todo: carpeta de datos, instalación y la versión a la que pertenece este `vmsctl`.

use crate::cli::{Args, CtlError};
use std::path::{Path, PathBuf};
use vms_common::layout::{default_data_dir, Component, DataLayout, InstallLayout};

pub struct Ctx {
    pub json: bool,
    pub data: DataLayout,
    install: Option<InstallLayout>,
    component: Option<Component>,
}

#[cfg(windows)]
fn registry(name: &str) -> Option<PathBuf> {
    crate::winreg::get_string(crate::winreg::PRODUCT_KEY, name)
        .ok()
        .flatten()
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
}

#[cfg(not(windows))]
fn registry(_name: &str) -> Option<PathBuf> {
    None
}

impl Ctx {
    /// `--data-dir` → `VMS_DATA_DIR` → registro (`DataDir`) → `%ProgramData%\VMSMultimarca`.
    /// `--install-dir` → la carpeta de este `vmsctl` → registro (`InstallDir`).
    pub fn from_args(a: &Args) -> Ctx {
        let data = a
            .value("--data-dir")
            .map(PathBuf::from)
            .or_else(|| std::env::var_os("VMS_DATA_DIR").filter(|v| !v.is_empty()).map(PathBuf::from))
            .or_else(|| registry("DataDir"))
            .unwrap_or_else(default_data_dir);
        let from_exe = std::env::current_exe().ok().and_then(|exe| InstallLayout::from_vmsctl_exe(&exe));
        let (install, component) = match (a.value("--install-dir"), from_exe) {
            (Some(dir), exe) => (Some(InstallLayout::new(dir)), exe.map(|(_, c)| c)),
            (None, Some((inst, comp))) => (Some(inst), Some(comp)),
            (None, None) => (registry("InstallDir").map(InstallLayout::new), None),
        };
        Ctx { json: a.has("--json"), data: DataLayout::new(data), install, component }
    }

    #[cfg(test)]
    pub fn for_tests(data: &Path, install: Option<&Path>, component: Option<Component>) -> Ctx {
        Ctx { json: true, data: DataLayout::new(data), install: install.map(InstallLayout::new), component }
    }

    pub fn install(&self) -> Result<&InstallLayout, CtlError> {
        self.install.as_ref().ok_or_else(|| {
            CtlError::usage(
                "no sé dónde está instalado el programa: ejecuta el vmsctl de versions\\<X>\\bin o indica --install-dir",
            )
        })
    }

    /// Versión a la que pertenece este `vmsctl` (`versions\<X>\bin`).
    pub fn own_version(&self) -> Option<&str> {
        match &self.component {
            Some(Component::Version { version, .. }) => Some(version),
            _ => None,
        }
    }

    /// Carpeta con `runtime\`, `app\` y `engine\` de este `vmsctl`.
    pub fn component_root(&self) -> Result<&Path, CtlError> {
        self.component.as_ref().map(Component::root).ok_or_else(|| {
            CtlError::usage(
                "este vmsctl no está dentro de versions\\<X>\\bin ni de updater\\slot-<x>: no sé qué runtime usar",
            )
        })
    }

    pub fn python(&self, override_path: Option<&str>) -> Result<PathBuf, CtlError> {
        if let Some(p) = override_path {
            return Ok(PathBuf::from(p));
        }
        let root = self.component_root()?;
        Ok(if cfg!(windows) {
            root.join("runtime").join("python.exe")
        } else {
            root.join("runtime").join("bin").join("python3")
        })
    }
}
