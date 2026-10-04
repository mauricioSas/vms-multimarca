//! Escritura atómica de archivos (CONTRATO §13.5): gemelo de `vms.core.atomic` en Python.
//!
//! Temporal en la misma carpeta → `sync_all` (FlushFileBuffers en Windows) → sustitución atómica:
//! `MoveFileExW(MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)` en Windows y `rename` + `fsync`
//! de la carpeta en POSIX. Un corte de luz deja el archivo anterior o el nuevo, nunca uno a medias.

use std::fs::{self, File};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

/// Reintentos ante un bloqueo momentáneo (antivirus, indexador) en Windows.
const REPLACE_RETRIES: u32 = 20;

/// Ruta del temporal que usa [`atomic_write`] para `path`.
pub fn temp_path(path: &Path) -> PathBuf {
    let name = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    path.with_file_name(format!("{name}.tmp-{}", std::process::id()))
}

/// Primer paso: escribe y persiste el temporal. Público para poder inyectar un fallo entre los dos pasos.
pub fn write_temp(path: &Path, data: &[u8]) -> io::Result<PathBuf> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)?;
        }
    }
    let tmp = temp_path(path);
    let mut f = File::create(&tmp)?;
    f.write_all(data)?;
    f.sync_all()?;
    Ok(tmp)
}

/// Segundo paso: sustituye `path` por `tmp` de forma atómica.
pub fn replace(tmp: &Path, path: &Path) -> io::Result<()> {
    let mut last: Option<io::Error> = None;
    for _ in 0..REPLACE_RETRIES {
        match replace_once(tmp, path) {
            Ok(()) => return Ok(()),
            Err(e) if e.kind() == io::ErrorKind::PermissionDenied => {
                last = Some(e);
                std::thread::sleep(std::time::Duration::from_millis(100));
            }
            Err(e) => {
                let _ = fs::remove_file(tmp);
                return Err(e);
            }
        }
    }
    let _ = fs::remove_file(tmp);
    Err(last.unwrap_or_else(|| io::Error::other("no se pudo sustituir el archivo")))
}

/// Escribe `data` en `path` de forma atómica.
pub fn atomic_write(path: &Path, data: &[u8]) -> io::Result<()> {
    let tmp = write_temp(path, data)?;
    replace(&tmp, path)
}

#[cfg(windows)]
fn replace_once(tmp: &Path, path: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH};
    let wide = |p: &Path| p.as_os_str().encode_wide().chain(std::iter::once(0)).collect::<Vec<u16>>();
    let (src, dst) = (wide(tmp), wide(path));
    // SAFETY: las dos cadenas son UTF-16 terminadas en cero y viven hasta el final de la llamada.
    let ok = unsafe { MoveFileExW(src.as_ptr(), dst.as_ptr(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) };
    if ok == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(not(windows))]
fn replace_once(tmp: &Path, path: &Path) -> io::Result<()> {
    fs::rename(tmp, path)?;
    if let Some(parent) = path.parent() {
        let dir = if parent.as_os_str().is_empty() { Path::new(".") } else { parent };
        // Algunos sistemas de archivos no admiten fsync de carpetas: no es un error.
        if let Ok(d) = File::open(dir) {
            let _ = d.sync_all();
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writes_and_replaces() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("state").join("active.json");
        atomic_write(&p, b"{\"active\":\"1.0.0\"}").unwrap();
        atomic_write(&p, b"{\"active\":\"2.0.0\"}").unwrap();
        assert_eq!(fs::read_to_string(&p).unwrap(), "{\"active\":\"2.0.0\"}");
        assert!(!temp_path(&p).exists(), "no debe quedar el temporal");
    }

    #[test]
    fn crash_between_write_and_rename_keeps_the_old_file() {
        // Fallo inyectado: el proceso «muere» tras escribir el temporal y antes de renombrar.
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("active.json");
        atomic_write(&p, b"viejo").unwrap();
        let tmp = write_temp(&p, b"nuevo-a-medias").unwrap();
        assert_eq!(fs::read(&p).unwrap(), b"viejo");
        // Al «reiniciar», el siguiente escritor pisa el temporal huérfano y termina bien.
        assert!(tmp.exists());
        atomic_write(&p, b"nuevo").unwrap();
        assert_eq!(fs::read(&p).unwrap(), b"nuevo");
    }
}
