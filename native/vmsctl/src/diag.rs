//! `vmsctl diag bundle --out <zip>`: registros y estado para soporte, **nunca secretos**.
//!
//! Entra: `logs\` (de cada registro, el actual y el `.1`, como mucho sus últimos 10 MB cada uno y 100 MB en
//! total; más `status-*.json`), `state\*.json`, `updater\public-status.json`,
//! `config\config.json` (no guarda contraseñas), `analytics\status.json`, el `.env` con los valores
//! sensibles tapados y el estado de los servicios y versiones. No entra: `secrets\`, `mediamtx\` (el YAML
//! lleva las URL de las cámaras con contraseña), grabaciones ni respaldos. Todo el texto pasa además por
//! la ocultación de credenciales.
//!
//! El ZIP se escribe aquí mismo (formato PKZIP 2.0, «deflate» con miniz_oxide): sin dependencias de C. Se
//! escribe archivo a archivo en un temporal (en memoria solo hay un archivo a la vez) y se sustituye al
//! final. Sin ZIP64: con los límites de arriba nunca se acerca a 4 GiB, y si llegara se corta con un error
//! claro en vez de escribir un ZIP roto.

use crate::cli::{CtlError, Outcome};
use crate::ctx::Ctx;
use serde_json::{json, Value};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

const MAX_FILE: u64 = 10 * 1024 * 1024;
const MAX_TOTAL: u64 = 100 * 1024 * 1024;
const SENSITIVE: &[&str] = &["TOKEN", "PASSWORD", "PASS", "SECRET", "KEY", "DSN"];

fn crc32_table() -> [u32; 256] {
    let mut t = [0u32; 256];
    for (i, slot) in t.iter_mut().enumerate() {
        let mut c = i as u32;
        for _ in 0..8 {
            c = if c & 1 != 0 { 0xEDB8_8320 ^ (c >> 1) } else { c >> 1 };
        }
        *slot = c;
    }
    t
}

pub fn crc32(data: &[u8]) -> u32 {
    let t = crc32_table();
    !data.iter().fold(!0u32, |c, b| t[((c ^ u32::from(*b)) & 0xff) as usize] ^ (c >> 8))
}

/// Escritor ZIP mínimo en flujo: cada entrada se escribe en cuanto se añade; solo el directorio central
/// (pequeño) queda en memoria.
pub struct ZipWriter<W: Write> {
    out: W,
    offset: u64,
    central: Vec<u8>,
    entries: u16,
}

fn too_big(what: &str) -> io::Error {
    io::Error::other(format!("el paquete de diagnóstico supera el límite del formato ZIP sin ZIP64 ({what})"))
}

impl<W: Write> ZipWriter<W> {
    pub fn new(out: W) -> Self {
        Self { out, offset: 0, central: Vec::new(), entries: 0 }
    }

    pub fn add(&mut self, name: &str, data: &[u8]) -> io::Result<()> {
        if self.entries == u16::MAX {
            return Err(too_big("más de 65535 archivos"));
        }
        let size = u32::try_from(data.len()).map_err(|_| too_big("un archivo de más de 4 GiB"))?;
        let crc = crc32(data);
        let deflated = miniz_oxide::deflate::compress_to_vec(data, 6);
        let (method, payload): (u16, &[u8]) = if deflated.len() < data.len() { (8, &deflated) } else { (0, data) };
        let name_b = name.as_bytes();
        let entry_len = 30 + name_b.len() as u64 + payload.len() as u64;
        // El directorio central y el final también tienen que caber por debajo de 4 GiB.
        let room = u64::from(u32::MAX) - self.central.len() as u64 - 46 - name_b.len() as u64 - 22;
        if self.offset + entry_len > room {
            return Err(too_big("más de 4 GiB"));
        }
        let offset = self.offset as u32;
        // Fecha DOS fija (1-ene-2026 00:00): el contenido ya lleva sus propias fechas.
        let (dos_time, dos_date): (u16, u16) = (0, ((2026 - 1980) << 9) | (1 << 5) | 1);
        let mut header = Vec::with_capacity(30 + name_b.len());
        header.extend_from_slice(&0x0403_4b50u32.to_le_bytes());
        header.extend_from_slice(&20u16.to_le_bytes());
        header.extend_from_slice(&0x0800u16.to_le_bytes()); // nombres en UTF-8
        header.extend_from_slice(&method.to_le_bytes());
        header.extend_from_slice(&dos_time.to_le_bytes());
        header.extend_from_slice(&dos_date.to_le_bytes());
        header.extend_from_slice(&crc.to_le_bytes());
        header.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        header.extend_from_slice(&size.to_le_bytes());
        header.extend_from_slice(&(name_b.len() as u16).to_le_bytes());
        header.extend_from_slice(&0u16.to_le_bytes());
        self.out.write_all(&header)?;
        self.out.write_all(name_b)?;
        self.out.write_all(payload)?;
        self.offset += entry_len;

        let c = &mut self.central;
        c.extend_from_slice(&0x0201_4b50u32.to_le_bytes());
        c.extend_from_slice(&20u16.to_le_bytes());
        c.extend_from_slice(&header[4..28]); // versión necesaria … longitud del nombre
        c.extend_from_slice(&0u16.to_le_bytes()); // extra
        c.extend_from_slice(&0u16.to_le_bytes()); // comentario
        c.extend_from_slice(&0u16.to_le_bytes()); // disco
        c.extend_from_slice(&0u16.to_le_bytes()); // atributos internos
        c.extend_from_slice(&0u32.to_le_bytes()); // atributos externos
        c.extend_from_slice(&offset.to_le_bytes());
        c.extend_from_slice(name_b);
        self.entries += 1;
        Ok(())
    }

    /// Escribe el directorio central y devuelve el escritor y el tamaño total.
    pub fn finish(mut self) -> io::Result<(W, u64)> {
        let cd_offset = self.offset as u32;
        let cd_size = self.central.len() as u32;
        self.out.write_all(&self.central)?;
        let mut end = Vec::with_capacity(22);
        end.extend_from_slice(&0x0605_4b50u32.to_le_bytes());
        end.extend_from_slice(&[0, 0, 0, 0]);
        end.extend_from_slice(&self.entries.to_le_bytes());
        end.extend_from_slice(&self.entries.to_le_bytes());
        end.extend_from_slice(&cd_size.to_le_bytes());
        end.extend_from_slice(&cd_offset.to_le_bytes());
        end.extend_from_slice(&0u16.to_le_bytes());
        self.out.write_all(&end)?;
        self.out.flush()?;
        let total = self.offset + self.central.len() as u64 + 22;
        Ok((self.out, total))
    }
}

/// ¿Entra este archivo de `logs\`? El registro actual y su `.1` (no `.2` … `.9`), y los `status-*.json`.
pub fn wanted_log(name: &str) -> bool {
    if name.starts_with("status-") && name.ends_with(".json") {
        return true;
    }
    match name.find(".log") {
        Some(i) => {
            let rest = &name[i + 4..];
            rest.is_empty() || rest == ".1"
        }
        None => false,
    }
}

/// Tapa los valores de las claves sensibles del `.env`.
pub fn mask_env(text: &str) -> String {
    text.lines()
        .map(|l| match l.split_once('=') {
            Some((k, v)) if !k.trim_start().starts_with('#') && !v.trim().is_empty() => {
                let up = k.to_ascii_uppercase();
                if SENSITIVE.iter().any(|s| up.contains(s)) {
                    format!("{k}=***")
                } else {
                    vms_common::redact(l).into_owned()
                }
            }
            _ => vms_common::redact(l).into_owned(),
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn read_tail(path: &Path, max: u64) -> std::io::Result<Vec<u8>> {
    let mut f = std::fs::File::open(path)?;
    let len = f.metadata()?.len();
    if len > max {
        f.seek(SeekFrom::Start(len - max))?;
    }
    let mut buf = Vec::new();
    f.take(max).read_to_end(&mut buf)?;
    Ok(buf)
}

fn redact_text(data: &[u8]) -> Vec<u8> {
    let text = String::from_utf8_lossy(data);
    text.lines().map(|l| vms_common::redact(l).into_owned()).collect::<Vec<_>>().join("\n").into_bytes()
}

fn files_in(dir: &Path, filter: impl Fn(&str) -> bool) -> Vec<PathBuf> {
    let mut out: Vec<PathBuf> = std::fs::read_dir(dir)
        .map(|rd| {
            rd.filter_map(Result::ok)
                .map(|e| e.path())
                .filter(|p| p.is_file() && p.file_name().and_then(|n| n.to_str()).is_some_and(&filter))
                .collect()
        })
        .unwrap_or_default();
    out.sort();
    out
}

pub fn bundle(ctx: &Ctx, out: &Path, services: Value) -> Result<Outcome, CtlError> {
    bundle_with_limits(ctx, out, services, MAX_FILE, MAX_TOTAL)
}

fn bundle_with_limits(
    ctx: &Ctx,
    out: &Path,
    services: Value,
    max_file: u64,
    max_total: u64,
) -> Result<Outcome, CtlError> {
    let io_err = |e: io::Error| CtlError::io(&e, &out.display().to_string());
    if let Some(parent) = out.parent().filter(|p| !p.as_os_str().is_empty()) {
        std::fs::create_dir_all(parent).map_err(io_err)?;
    }
    let tmp = vms_common::atomic::temp_path(out);
    let file = std::fs::File::create(&tmp).map_err(io_err)?;
    let result = (|| -> io::Result<(Vec<String>, Vec<String>, u64)> {
        let mut zip = ZipWriter::new(io::BufWriter::new(file));
        let d = &ctx.data;
        let mut names = Vec::new();
        let mut skipped = Vec::new();
        let mut budget = max_total;
        // Los registros, del más reciente al más antiguo dentro de cada nombre (el actual antes que el .1).
        for p in files_in(&d.logs_dir(), wanted_log) {
            let name = p.file_name().unwrap_or_default().to_string_lossy().into_owned();
            if budget == 0 {
                skipped.push(name);
                continue;
            }
            if let Ok(data) = read_tail(&p, max_file.min(budget)) {
                budget = budget.saturating_sub(data.len() as u64);
                zip.add(&format!("logs/{name}"), &redact_text(&data))?;
                names.push(format!("logs/{name}"));
            }
        }
        for (prefix, dir) in [("state", d.state_dir()), ("state/requests", d.state().requests_dir())] {
            for p in files_in(&dir, |n| n.ends_with(".json")) {
                if let Ok(data) = read_tail(&p, max_file) {
                    let name = format!("{prefix}/{}", p.file_name().unwrap_or_default().to_string_lossy());
                    zip.add(&name, &redact_text(&data))?;
                    names.push(name);
                }
            }
        }
        for (name, path) in [
            ("updater/public-status.json", d.updater_dir().join("public-status.json")),
            ("config/config.json", d.config_dir().join("config.json")),
            ("analytics/status.json", d.analytics_dir().join("status.json")),
        ] {
            if let Ok(data) = read_tail(&path, max_file) {
                zip.add(name, &redact_text(&data))?;
                names.push(name.to_string());
            }
        }
        if let Ok(text) = std::fs::read_to_string(d.env_file()) {
            zip.add("env.txt", mask_env(&text).as_bytes())?;
            names.push("env.txt".into());
        }
        let versions = ctx.install().map(|i| i.installed_versions()).unwrap_or_default();
        let meta = json!({
            "vmsctl": env!("CARGO_PKG_VERSION"), "created": vms_common::logfile::utc_now_iso(),
            "data_dir": d.root, "own_version": ctx.own_version(), "installed_versions": versions,
            "services": services, "logs_skipped_over_limit": skipped,
        });
        zip.add("vmsctl.json", &serde_json::to_vec_pretty(&meta).unwrap_or_default())?;
        names.push("vmsctl.json".into());
        let (w, total) = zip.finish()?;
        let f = w.into_inner().map_err(|e| e.into_error())?;
        f.sync_all()?;
        Ok((names, skipped, total))
    })();
    let (names, skipped, total) = match result {
        Ok(r) => r,
        Err(e) => {
            let _ = std::fs::remove_file(&tmp);
            return Err(io_err(e));
        }
    };
    vms_common::atomic::replace(&tmp, out).map_err(io_err)?;
    let mut text = format!("Informe de diagnóstico: {} ({} archivos, sin secretos).", out.display(), names.len());
    if !skipped.is_empty() {
        text.push_str(&format!(" {} registros no entraron por el límite de tamaño.", skipped.len()));
    }
    Ok(Outcome::new(json!({"out": out, "files": names, "bytes": total, "logs_skipped": skipped}), text))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Lector mínimo para comprobar el ZIP: (nombre, contenido descomprimido).
    fn read_zip(bytes: &[u8]) -> Vec<(String, Vec<u8>)> {
        let mut out = Vec::new();
        let mut i = 0;
        while i + 30 <= bytes.len() && bytes[i..i + 4] == 0x0403_4b50u32.to_le_bytes() {
            let u16at = |o: usize| u16::from_le_bytes([bytes[o], bytes[o + 1]]) as usize;
            let u32at = |o: usize| u32::from_le_bytes([bytes[o], bytes[o + 1], bytes[o + 2], bytes[o + 3]]);
            let method = u16at(i + 8);
            let crc = u32at(i + 14);
            let csize = u32at(i + 18) as usize;
            let nlen = u16at(i + 26);
            let name = String::from_utf8(bytes[i + 30..i + 30 + nlen].to_vec()).unwrap();
            let start = i + 30 + nlen;
            let payload = &bytes[start..start + csize];
            let data =
                if method == 8 { miniz_oxide::inflate::decompress_to_vec(payload).unwrap() } else { payload.to_vec() };
            assert_eq!(crc32(&data), crc, "CRC de {name}");
            out.push((name, data));
            i = start + csize;
        }
        assert_eq!(&bytes[bytes.len() - 22..bytes.len() - 18], &0x0605_4b50u32.to_le_bytes());
        out
    }

    #[test]
    fn only_current_and_first_backup_of_each_log() {
        assert!(wanted_log("engine.log") && wanted_log("engine.log.1") && wanted_log("vms.log"));
        assert!(!wanted_log("engine.log.2") && !wanted_log("audit.log.20") && !wanted_log("notas.txt"));
        assert!(wanted_log("status-VMSBackend.json"));
    }

    #[test]
    fn total_size_is_capped_and_memory_stays_per_file() {
        // Regresión: se cargaban en memoria todos los registros (también .2 … .9), sin tope total.
        let d = tempfile::tempdir().unwrap();
        let ctx = Ctx::for_tests(d.path(), None, None);
        std::fs::create_dir_all(ctx.data.logs_dir()).unwrap();
        for name in ["a.log", "a.log.1", "a.log.2", "b.log", "c.log"] {
            std::fs::write(ctx.data.logs_dir().join(name), vec![b'x'; 3000]).unwrap();
        }
        let out = d.path().join("sub").join("diag.zip");
        let res = bundle_with_limits(&ctx, &out, json!(null), 2000, 5000).unwrap();
        let files = read_zip(&std::fs::read(&out).unwrap());
        let logs: Vec<(&str, usize)> =
            files.iter().filter(|(n, _)| n.starts_with("logs/")).map(|(n, c)| (n.as_str(), c.len())).collect();
        assert_eq!(logs, [("logs/a.log", 2000), ("logs/a.log.1", 2000), ("logs/b.log", 1000)]);
        assert_eq!(res.data["logs_skipped"], json!(["c.log"]));
        assert!(res.text.contains("límite"));
        assert!(!vms_common::atomic::temp_path(&out).exists());
    }

    #[test]
    fn zip_refuses_to_pass_the_u32_limits() {
        let mut z = ZipWriter::new(io::sink());
        z.offset = u64::from(u32::MAX) - 10;
        let e = z.add("x", b"abc").unwrap_err();
        assert!(e.to_string().contains("4 GiB"), "{e}");
        let mut z = ZipWriter::new(io::sink());
        z.entries = u16::MAX;
        assert!(z.add("x", b"a").unwrap_err().to_string().contains("65535"));
    }

    #[test]
    fn crc32_known_value() {
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
    }

    #[test]
    fn env_values_are_masked() {
        let m =
            mask_env("VMS_KIOSK_TOKEN=abc\nVMS_PG_DSN=postgresql://u:p@h/db\nVMS_HTTP_PORT=8600\n# VMS_SECRET_KEY=x");
        assert!(m.contains("VMS_KIOSK_TOKEN=***") && m.contains("VMS_PG_DSN=***") && m.contains("VMS_HTTP_PORT=8600"));
        assert!(!m.contains("abc") && !m.contains("u:p@"));
    }

    #[test]
    fn bundle_has_logs_and_state_but_never_secrets_or_the_engine_yaml() {
        let d = tempfile::tempdir().unwrap();
        let ctx = Ctx::for_tests(d.path(), None, None);
        let data = &ctx.data;
        for dir in data.all_dirs() {
            std::fs::create_dir_all(dir).unwrap();
        }
        std::fs::write(data.logs_dir().join("engine.log"), "ERR rtsp://admin:Cl4ve%21@10.0.0.1/x\n".repeat(200))
            .unwrap();
        std::fs::write(data.state().pointer_path(), r#"{"schema":1,"active":"2.0.0"}"#).unwrap();
        std::fs::write(data.secrets_dir().join("secret.key"), "NO-DEBE-SALIR").unwrap();
        std::fs::write(data.mediamtx_yml(), "paths:\n  cam/main:\n    source: rtsp://admin:Cl4ve%21@10.0.0.1/x\n")
            .unwrap();
        std::fs::write(data.env_file(), "VMS_KIOSK_TOKEN=NO-DEBE-SALIR\nVMS_HTTP_PORT=8600\n").unwrap();
        let out = d.path().join("diag.zip");
        let res = bundle(&ctx, &out, json!([])).unwrap();
        let files = read_zip(&std::fs::read(&out).unwrap());
        let names: Vec<&str> = files.iter().map(|(n, _)| n.as_str()).collect();
        assert!(
            names.contains(&"logs/engine.log") && names.contains(&"state/active.json") && names.contains(&"env.txt")
        );
        assert!(!names.iter().any(|n| n.contains("secret") || n.contains("mediamtx")));
        for (name, content) in &files {
            let text = String::from_utf8_lossy(content);
            assert!(!text.contains("NO-DEBE-SALIR") && !text.contains("Cl4ve"), "{name} deja ver un secreto");
        }
        assert_eq!(res.data["files"].as_array().unwrap().len(), files.len());
    }
}
