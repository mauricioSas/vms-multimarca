//! Registro con rotación y ocultación de credenciales (CONTRATO §14.1: `logs\<servicio>.log`, 10 × 10 MB).
//!
//! - Cada línea pasa por [`crate::redact`] antes de tocar el disco.
//! - Rotación como `RotatingFileHandler` de Python: `x.log` → `x.log.1` → … → `x.log.<backups>`.
//! - En Windows, quien lea el registro (el backend sigue `engine.log` con `vms/engine/logtail.py`) puede
//!   tenerlo abierto justo al rotar: si el renombrado falla, se sigue escribiendo en el actual y se vuelve
//!   a intentar más tarde. Rust abre los archivos con `FILE_SHARE_DELETE`, así que el escritor no bloquea.
//! - [`RotatingLog::copy_truncate`] (solo `engine.log`): copia el actual a `x.log.1` y lo vacía, **sin
//!   borrarlo ni renombrarlo nunca**. Así el archivo que sigue el backend conserva siempre su ACL (solo
//!   VMSEngine escribe) y ningún otro servicio puede recrearlo en el instante de la rotación para meter
//!   líneas falsas (un 401 inventado pausaría cámaras). Si `x.log.1` no se puede sustituir (p. ej. alguien
//!   lo creó antes con otro dueño), no se vacía el actual: no se pierde nada y el lector nunca lee esa copia.

use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const DEFAULT_MAX_BYTES: u64 = 10 * 1024 * 1024;
pub const DEFAULT_BACKUPS: u32 = 9;
/// Una línea más larga se corta (una salida sin saltos de línea no debe llenar la memoria).
pub const MAX_LINE: usize = 64 * 1024;
const ROTATE_RETRY: Duration = Duration::from_secs(10);

/// Fecha UTC «AAAA-MM-DDTHH:MM:SSZ» sin dependencias (algoritmo «days from civil» de H. Hinnant).
pub fn utc_iso(secs: u64) -> String {
    let days = (secs / 86_400) as i64;
    let rem = secs % 86_400;
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z", rem / 3600, (rem % 3600) / 60, rem % 60)
}

pub fn utc_now_iso() -> String {
    utc_iso(SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0))
}

pub struct RotatingLog {
    path: PathBuf,
    max_bytes: u64,
    backups: u32,
    file: Option<File>,
    size: u64,
    last_rotate_fail: Option<Instant>,
    copy_truncate: bool,
}

impl RotatingLog {
    pub fn new(path: impl Into<PathBuf>, max_bytes: u64, backups: u32) -> Self {
        Self {
            path: path.into(),
            max_bytes: max_bytes.max(1024),
            backups,
            file: None,
            size: 0,
            last_rotate_fail: None,
            copy_truncate: false,
        }
    }

    pub fn standard(path: impl Into<PathBuf>) -> Self {
        Self::new(path, DEFAULT_MAX_BYTES, DEFAULT_BACKUPS)
    }

    /// Rotación copiando y vaciando (ver la cabecera del módulo). Al menos una copia (`x.log.1`).
    pub fn copy_truncate(path: impl Into<PathBuf>, max_bytes: u64, backups: u32) -> Self {
        let mut log = Self::new(path, max_bytes, backups.max(1));
        log.copy_truncate = true;
        log
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    fn open(&mut self) -> io::Result<&mut File> {
        if self.file.is_none() {
            if let Some(parent) = self.path.parent() {
                fs::create_dir_all(parent)?;
            }
            let f = OpenOptions::new().create(true).append(true).open(&self.path)?;
            self.size = f.metadata().map(|m| m.len()).unwrap_or(0);
            self.file = Some(f);
        }
        Ok(self.file.as_mut().expect("abierto"))
    }

    fn backup(&self, n: u32) -> PathBuf {
        let mut s = self.path.clone().into_os_string();
        s.push(format!(".{n}"));
        PathBuf::from(s)
    }

    fn rotate(&mut self) {
        if let Some(t) = self.last_rotate_fail {
            if t.elapsed() < ROTATE_RETRY {
                return;
            }
        }
        let copy = self.copy_truncate;
        if !copy {
            self.file = None; // cerrar antes de renombrar (Windows)
        }
        let result = (|| -> io::Result<()> {
            if self.backups == 0 {
                return fs::remove_file(&self.path);
            }
            let last = self.backup(self.backups);
            if last.exists() {
                fs::remove_file(&last)?;
            }
            for n in (1..self.backups).rev() {
                let from = self.backup(n);
                if from.exists() {
                    fs::rename(&from, self.backup(n + 1))?;
                }
            }
            if !copy {
                return fs::rename(&self.path, self.backup(1));
            }
            let first = self.backup(1);
            if first.exists() {
                // No se pudo apartar (otro dueño): no se vacía el actual.
                return Err(io::Error::new(io::ErrorKind::AlreadyExists, "la copia .1 sigue ahí"));
            }
            let mut tmp = self.path.clone().into_os_string();
            tmp.push(format!(".rot-{}", std::process::id()));
            let tmp = PathBuf::from(tmp);
            fs::copy(&self.path, &tmp)?;
            if let Err(e) = fs::rename(&tmp, &first) {
                let _ = fs::remove_file(&tmp);
                return Err(e);
            }
            // Vaciar con un manejador de escritura (el de añadir no basta en Windows); el de añadir sigue
            // escribiendo al final, que ahora es 0.
            OpenOptions::new().write(true).open(&self.path)?.set_len(0)
        })();
        match result {
            Ok(()) => {
                self.size = 0;
                self.last_rotate_fail = None;
            }
            Err(_) => self.last_rotate_fail = Some(Instant::now()),
        }
    }

    /// Escribe una línea tal cual (ya sin credenciales) más el salto de línea.
    pub fn write_line(&mut self, line: &str) -> io::Result<()> {
        let mut line = crate::redact(line).into_owned();
        if line.len() > MAX_LINE {
            let mut cut = MAX_LINE;
            while !line.is_char_boundary(cut) {
                cut -= 1;
            }
            line.truncate(cut);
            line.push_str(" […]");
        }
        line.push('\n');
        if self.size > 0 && self.size + line.len() as u64 > self.max_bytes {
            self.rotate();
        }
        let f = self.open()?;
        f.write_all(line.as_bytes())?;
        f.flush()?;
        self.size += line.len() as u64;
        Ok(())
    }

    /// Línea de evento propio con fecha UTC delante (los registros de los hijos van sin fecha añadida:
    /// MediaMTX ya la pone y `logtail.py` la necesita al principio).
    pub fn event(&mut self, msg: &str) {
        let _ = self.write_line(&format!("{} {msg}", utc_now_iso()));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn utc_iso_matches_known_dates() {
        assert_eq!(utc_iso(0), "1970-01-01T00:00:00Z");
        assert_eq!(utc_iso(951_782_400), "2000-02-29T00:00:00Z");
        assert_eq!(utc_iso(1_763_600_000), "2025-11-20T00:53:20Z");
        assert_eq!(utc_iso(1_791_158_399), "2026-10-04T23:59:59Z");
    }

    #[test]
    fn lines_are_redacted_and_rotated() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("logs").join("engine.log");
        let mut log = RotatingLog::new(&p, 1024, 2);
        log.write_line("ERR [path cam/main] rtsp://admin:Cl4ve%21@10.0.0.1:554/x 401").unwrap();
        let text = fs::read_to_string(&p).unwrap();
        assert!(text.contains("rtsp://***:***@10.0.0.1") && !text.contains("Cl4ve"), "{text}");
        for i in 0..200 {
            log.write_line(&format!("línea {i:03} {}", "x".repeat(40))).unwrap();
        }
        assert!(p.exists() && log.backup(1).exists() && log.backup(2).exists());
        assert!(!log.backup(3).exists(), "solo se guardan 2 copias");
        for f in [p.clone(), log.backup(1), log.backup(2)] {
            assert!(fs::metadata(&f).unwrap().len() <= 1024 + 64, "{} demasiado grande", f.display());
        }
        let last = fs::read_to_string(&p).unwrap();
        assert!(last.contains("línea 199"));
    }

    #[test]
    fn copy_truncate_never_replaces_the_live_file() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("engine.log");
        let mut log = RotatingLog::copy_truncate(&p, 1024, 3);
        log.write_line("primera").unwrap();
        #[cfg(unix)]
        let ino = {
            use std::os::unix::fs::MetadataExt;
            fs::metadata(&p).unwrap().ino()
        };
        for i in 0..100 {
            log.write_line(&format!("línea {i:03} {}", "x".repeat(40))).unwrap();
        }
        assert!(log.backup(1).exists() && log.backup(2).exists());
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            assert_eq!(fs::metadata(&p).unwrap().ino(), ino, "engine.log es siempre el mismo archivo");
        }
        let all: String = [log.backup(3), log.backup(2), log.backup(1), p.clone()]
            .iter()
            .map(|f| fs::read_to_string(f).unwrap_or_default())
            .collect();
        assert!(all.contains("línea 099") && fs::read_to_string(&p).unwrap().contains("línea 099"));
        assert!(fs::metadata(&p).unwrap().len() <= 1024 + 64);
        assert!(!d.path().read_dir().unwrap().any(|e| e.unwrap().file_name().to_string_lossy().contains(".rot-")));
    }

    #[test]
    fn copy_truncate_keeps_writing_when_the_backup_cannot_be_replaced() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("engine.log");
        // Una copia .1 que no se puede borrar ni sustituir (aquí, una carpeta no vacía; en Windows, un archivo
        // de otro dueño): la rotación falla y el actual no se vacía.
        let mut log = RotatingLog::copy_truncate(&p, 1024, 1);
        fs::create_dir(log.backup(1)).unwrap();
        fs::write(log.backup(1).join("x"), b"no vacia").unwrap();
        for i in 0..60 {
            log.write_line(&format!("línea {i:03} {}", "x".repeat(40))).unwrap();
        }
        let text = fs::read_to_string(&p).unwrap();
        assert!(text.contains("línea 000") && text.contains("línea 059"), "no se vació ni se perdió nada");
    }

    #[test]
    fn huge_line_is_cut_on_a_char_boundary() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("x.log");
        let mut log = RotatingLog::standard(&p);
        log.write_line(&"ñ".repeat(MAX_LINE)).unwrap();
        let text = fs::read_to_string(&p).unwrap();
        assert!(text.len() <= MAX_LINE + 16 && text.ends_with("[…]\n"));
    }

    #[test]
    fn event_lines_have_a_timestamp() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("x.log");
        let mut log = RotatingLog::standard(&p);
        log.event("arranco token=abc123");
        let text = fs::read_to_string(&p).unwrap();
        assert!(text.starts_with("20") && text.contains("Z arranco token=***"), "{text}");
    }
}
