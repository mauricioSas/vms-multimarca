//! `vmsctl diag bundle --out <zip>`: registros y estado para soporte, **nunca secretos**.
//!
//! Entra: `logs\` (cada archivo, como mucho sus últimos 20 MB), `state\*.json`, `updater\public-status.json`,
//! `config\config.json` (no guarda contraseñas), `analytics\status.json`, el `.env` con los valores
//! sensibles tapados y el estado de los servicios y versiones. No entra: `secrets\`, `mediamtx\` (el YAML
//! lleva las URL de las cámaras con contraseña), grabaciones ni respaldos. Todo el texto pasa además por
//! la ocultación de credenciales.
//!
//! El ZIP se escribe aquí mismo (formato PKZIP 2.0, «deflate» con miniz_oxide): sin dependencias de C.

use crate::cli::{CtlError, Outcome};
use crate::ctx::Ctx;
use serde_json::{json, Value};
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};

const MAX_FILE: u64 = 20 * 1024 * 1024;
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

/// Escritor ZIP mínimo (todo en memoria; el paquete es pequeño).
#[derive(Default)]
pub struct ZipWriter {
    body: Vec<u8>,
    central: Vec<u8>,
    entries: u16,
}

impl ZipWriter {
    pub fn add(&mut self, name: &str, data: &[u8]) {
        let crc = crc32(data);
        let deflated = miniz_oxide::deflate::compress_to_vec(data, 6);
        let (method, payload): (u16, &[u8]) = if deflated.len() < data.len() { (8, &deflated) } else { (0, data) };
        let offset = self.body.len() as u32;
        let name_b = name.as_bytes();
        // Fecha DOS fija (1-ene-2026 00:00): el contenido ya lleva sus propias fechas.
        let (dos_time, dos_date): (u16, u16) = (0, ((2026 - 1980) << 9) | (1 << 5) | 1);
        let mut header = Vec::new();
        header.extend_from_slice(&0x0403_4b50u32.to_le_bytes());
        header.extend_from_slice(&20u16.to_le_bytes());
        header.extend_from_slice(&0x0800u16.to_le_bytes()); // nombres en UTF-8
        header.extend_from_slice(&method.to_le_bytes());
        header.extend_from_slice(&dos_time.to_le_bytes());
        header.extend_from_slice(&dos_date.to_le_bytes());
        header.extend_from_slice(&crc.to_le_bytes());
        header.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        header.extend_from_slice(&(data.len() as u32).to_le_bytes());
        header.extend_from_slice(&(name_b.len() as u16).to_le_bytes());
        header.extend_from_slice(&0u16.to_le_bytes());
        self.body.extend_from_slice(&header);
        self.body.extend_from_slice(name_b);
        self.body.extend_from_slice(payload);

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
    }

    pub fn finish(mut self) -> Vec<u8> {
        let cd_offset = self.body.len() as u32;
        let cd_size = self.central.len() as u32;
        self.body.extend_from_slice(&self.central);
        self.body.extend_from_slice(&0x0605_4b50u32.to_le_bytes());
        self.body.extend_from_slice(&[0, 0, 0, 0]);
        self.body.extend_from_slice(&self.entries.to_le_bytes());
        self.body.extend_from_slice(&self.entries.to_le_bytes());
        self.body.extend_from_slice(&cd_size.to_le_bytes());
        self.body.extend_from_slice(&cd_offset.to_le_bytes());
        self.body.extend_from_slice(&0u16.to_le_bytes());
        self.body
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

fn read_tail(path: &Path) -> std::io::Result<Vec<u8>> {
    let mut f = std::fs::File::open(path)?;
    let len = f.metadata()?.len();
    if len > MAX_FILE {
        f.seek(SeekFrom::Start(len - MAX_FILE))?;
    }
    let mut buf = Vec::new();
    f.read_to_end(&mut buf)?;
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
    let d = &ctx.data;
    let mut zip = ZipWriter::default();
    let mut names = Vec::new();
    let mut add = |zip: &mut ZipWriter, name: String, data: Vec<u8>| {
        zip.add(&name, &data);
        names.push(name);
    };
    for p in files_in(&d.logs_dir(), |n| n.contains(".log")) {
        if let Ok(data) = read_tail(&p) {
            add(&mut zip, format!("logs/{}", p.file_name().unwrap_or_default().to_string_lossy()), redact_text(&data));
        }
    }
    for (prefix, dir) in [("state", d.state_dir()), ("state/requests", d.state().requests_dir())] {
        for p in files_in(&dir, |n| n.ends_with(".json")) {
            if let Ok(data) = std::fs::read(&p) {
                add(
                    &mut zip,
                    format!("{prefix}/{}", p.file_name().unwrap_or_default().to_string_lossy()),
                    redact_text(&data),
                );
            }
        }
    }
    for (name, path) in [
        ("updater/public-status.json", d.updater_dir().join("public-status.json")),
        ("config/config.json", d.config_dir().join("config.json")),
        ("analytics/status.json", d.analytics_dir().join("status.json")),
    ] {
        if let Ok(data) = std::fs::read(&path) {
            add(&mut zip, name.to_string(), redact_text(&data));
        }
    }
    if let Ok(text) = std::fs::read_to_string(d.env_file()) {
        add(&mut zip, "env.txt".into(), mask_env(&text).into_bytes());
    }
    let versions = ctx.install().map(|i| i.installed_versions()).unwrap_or_default();
    let meta = json!({
        "vmsctl": env!("CARGO_PKG_VERSION"), "created": vms_common::logfile::utc_now_iso(),
        "data_dir": d.root, "own_version": ctx.own_version(), "installed_versions": versions, "services": services,
    });
    add(&mut zip, "vmsctl.json".into(), serde_json::to_vec_pretty(&meta).unwrap_or_default());
    let bytes = zip.finish();
    vms_common::atomic_write(out, &bytes).map_err(|e| CtlError::io(&e, &out.display().to_string()))?;
    Ok(Outcome::new(
        json!({"out": out, "files": names, "bytes": bytes.len()}),
        format!("Informe de diagnóstico: {} ({} archivos, sin secretos).", out.display(), names.len()),
    ))
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
