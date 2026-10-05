//! Registro del visor: `%APPDATA%\VMSMultimarca\visor.log` (1 MB, más una copia `.1`). Todo pasa por
//! `vms_common::redact` (URL con contraseña, tokens): el token de kiosco no se registra nunca.

use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;

const MAX_BYTES: u64 = 1024 * 1024;

static FILE: Mutex<Option<PathBuf>> = Mutex::new(None);

pub fn init(path: PathBuf) {
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if let Ok(mut f) = FILE.lock() {
        *f = Some(path);
    }
}

fn write(level: &str, msg: &str) {
    let clean = vms_common::redact(msg);
    let secs =
        std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or_default();
    let line = format!("{secs} {level} {clean}\n");
    if cfg!(debug_assertions) {
        eprint!("{line}");
    }
    let Ok(guard) = FILE.lock() else { return };
    let Some(path) = guard.as_ref() else { return };
    if std::fs::metadata(path).map(|m| m.len() > MAX_BYTES).unwrap_or(false) {
        let _ = std::fs::rename(path, path.with_extension("log.1"));
    }
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
        let _ = f.write_all(line.as_bytes());
    }
}

pub fn info(msg: impl AsRef<str>) {
    write("INFO", msg.as_ref());
}

pub fn warn(msg: impl AsRef<str>) {
    write("AVISO", msg.as_ref());
}

pub fn error(msg: impl AsRef<str>) {
    write("ERROR", msg.as_ref());
}

#[cfg(test)]
mod tests {
    #[test]
    fn redacts_credentials_in_urls() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("visor.log");
        super::init(p.clone());
        super::warn("fallo con rtsp://admin:Secreta1@10.0.0.2:554/x");
        let text = std::fs::read_to_string(&p).unwrap();
        assert!(!text.contains("Secreta1"), "{text}");
        assert!(text.contains("AVISO"));
    }
}
