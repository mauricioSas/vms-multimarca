//! «Hay una versión nueva → Descargar e instalar» desde el visor, con las versiones publicadas en GitHub Releases
//! (docs/PUBLICAR-VERSION.md §«Publicar en GitHub»). Es el camino mientras no hay claves de publicación para el
//! actualizador automático (TUF): la persona lo pide, el visor descarga el instalador, comprueba su SHA-256 con el
//! `SHA256SUMS.txt` de la misma versión y lo lanza con elevación (UAC) en modo silencioso con barra de progreso.
//! La instalación encima conserva la configuración y las grabaciones.
//!
//! Las descargas van con `curl.exe` de Windows (Schannel y los certificados del sistema; viene con Windows 10/11):
//! así el visor no lleva otra pila TLS con su propio almacén de raíces.

use std::cmp::Ordering;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::Duration;

use serde::{Deserialize, Serialize};

pub const REPO: &str = "mauricioSas/vms-multimarca";
pub const RELEASES_API: &str = "https://api.github.com/repos/mauricioSas/vms-multimarca/releases?per_page=20";
/// Solo se descarga de aquí (la API devuelve estas URL; cualquier otra se rechaza).
const DOWNLOAD_PREFIX: &str = "https://github.com/mauricioSas/vms-multimarca/releases/download/";
const SUMS: &str = "SHA256SUMS.txt";
const MAX_INSTALLER: u64 = 1 << 30;

#[derive(Debug, Deserialize)]
struct Release {
    tag_name: String,
    #[serde(default)]
    draft: bool,
    #[serde(default)]
    prerelease: bool,
    #[serde(default)]
    html_url: String,
    #[serde(default)]
    assets: Vec<Asset>,
}

#[derive(Debug, Deserialize)]
struct Asset {
    name: String,
    browser_download_url: String,
    #[serde(default)]
    size: u64,
}

/// Versión nueva disponible para este puesto.
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct Novedad {
    pub version: String,
    pub notas_url: String,
    pub instalador: String,
    pub instalador_url: String,
    pub sumas_url: String,
    pub tamano: u64,
}

/// Compara `X.Y.Z[-pre]` como SemVer: núcleo numérico; una versión sin «-pre» va después de sus pre-versiones;
/// las pre-versiones se comparan por partes separadas por puntos (números como números).
pub fn compare(a: &str, b: &str) -> Ordering {
    fn split(v: &str) -> (Vec<u64>, Option<&str>) {
        let v = v.trim_start_matches('v');
        let v = v.split('+').next().unwrap_or(v);
        let (core, pre) = match v.split_once('-') {
            Some((c, p)) => (c, Some(p)),
            None => (v, None),
        };
        (core.split('.').map(|p| p.parse().unwrap_or(0)).collect(), pre)
    }
    let (ca, pa) = split(a);
    let (cb, pb) = split(b);
    let n = ca.len().max(cb.len());
    for i in 0..n {
        let x = ca.get(i).copied().unwrap_or(0);
        let y = cb.get(i).copied().unwrap_or(0);
        if x != y {
            return x.cmp(&y);
        }
    }
    match (pa, pb) {
        (None, None) => Ordering::Equal,
        (None, Some(_)) => Ordering::Greater,
        (Some(_), None) => Ordering::Less,
        (Some(pa), Some(pb)) => {
            let (ia, ib): (Vec<&str>, Vec<&str>) = (pa.split('.').collect(), pb.split('.').collect());
            for (x, y) in ia.iter().zip(ib.iter()) {
                let o = match (x.parse::<u64>(), y.parse::<u64>()) {
                    (Ok(x), Ok(y)) => x.cmp(&y),
                    (Ok(_), Err(_)) => Ordering::Less,
                    (Err(_), Ok(_)) => Ordering::Greater,
                    (Err(_), Err(_)) => x.cmp(y),
                };
                if o != Ordering::Equal {
                    return o;
                }
            }
            ia.len().cmp(&ib.len())
        }
    }
}

fn is_prerelease(v: &str) -> bool {
    v.trim_start_matches('v').contains('-')
}

/// La versión publicada más nueva que la instalada. Un puesto con una versión de prueba (beta) también recibe
/// versiones de prueba; uno con una versión final, solo versiones finales.
pub fn pick(releases_json: &[u8], installed: &str) -> Result<Option<Novedad>, String> {
    let releases: Vec<Release> =
        serde_json::from_slice(releases_json).map_err(|e| format!("respuesta de GitHub no válida: {e}"))?;
    let allow_pre = is_prerelease(installed);
    let mut best: Option<Novedad> = None;
    for r in releases {
        if r.draft || (r.prerelease && !allow_pre) {
            continue;
        }
        let version = r.tag_name.trim_start_matches('v').to_string();
        if !crate::paths::safe_version(&version) || compare(&version, installed) != Ordering::Greater {
            continue;
        }
        if best.as_ref().is_some_and(|b| compare(&version, &b.version) != Ordering::Greater) {
            continue;
        }
        let name = format!("VMSMultimarca-Setup-{version}.exe");
        let exe = r.assets.iter().find(|a| a.name == name);
        let sums = r.assets.iter().find(|a| a.name == SUMS);
        let (Some(exe), Some(sums)) = (exe, sums) else { continue };
        if !exe.browser_download_url.starts_with(DOWNLOAD_PREFIX)
            || !sums.browser_download_url.starts_with(DOWNLOAD_PREFIX)
            || exe.size > MAX_INSTALLER
        {
            continue;
        }
        best = Some(Novedad {
            version,
            notas_url: if r.html_url.starts_with("https://github.com/") {
                r.html_url
            } else {
                format!("https://github.com/{REPO}/releases")
            },
            instalador: name,
            instalador_url: exe.browser_download_url.clone(),
            sumas_url: sums.browser_download_url.clone(),
            tamano: exe.size,
        });
    }
    Ok(best)
}

/// SHA-256 (hex, minúsculas) de `file` según un `SHA256SUMS.txt` («<hex>  <nombre>» o «<hex> *<nombre>»).
pub fn expected_sha256(sums: &str, file: &str) -> Option<String> {
    sums.lines().find_map(|line| {
        let mut parts = line.split_whitespace();
        let hex = parts.next()?;
        let name = parts.next()?.trim_start_matches('*');
        (name == file && hex.len() == 64 && hex.chars().all(|c| c.is_ascii_hexdigit()))
            .then(|| hex.to_ascii_lowercase())
    })
}

pub fn sha256_file(path: &Path) -> std::io::Result<String> {
    use std::io::Read;
    let mut f = std::fs::File::open(path)?;
    let mut ctx = ring::digest::Context::new(&ring::digest::SHA256);
    let mut buf = vec![0u8; 1 << 16];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        ctx.update(&buf[..n]);
    }
    Ok(ctx.finish().as_ref().iter().map(|b| format!("{b:02x}")).collect())
}

fn curl() -> PathBuf {
    #[cfg(windows)]
    {
        let sys = std::env::var_os("SystemRoot").unwrap_or_else(|| "C:\\Windows".into());
        PathBuf::from(sys).join("System32").join("curl.exe")
    }
    #[cfg(not(windows))]
    {
        PathBuf::from("curl")
    }
}

fn curl_base(timeout: Duration) -> Command {
    let mut c = Command::new(curl());
    c.args(["--fail", "--silent", "--show-error", "--location", "--proto", "=https", "--max-redirs", "5"])
        .args(["--max-time", &timeout.as_secs().to_string()])
        .args(["-A", concat!("VMS-visor/", env!("CARGO_PKG_VERSION"))]);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        c.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    c
}

fn fetch(url: &str, timeout: Duration) -> Result<Vec<u8>, String> {
    let out = curl_base(timeout)
        .args(["-H", "Accept: application/vnd.github+json", url])
        .output()
        .map_err(|e| format!("no se pudo ejecutar curl: {e}"))?;
    if !out.status.success() {
        return Err(sin_conexion(&String::from_utf8_lossy(&out.stderr)));
    }
    Ok(out.stdout)
}

fn sin_conexion(detalle: &str) -> String {
    let d = detalle.trim();
    format!(
        "No se pudo hablar con GitHub ({}). Comprueba que este equipo tiene acceso a Internet.",
        if d.is_empty() { "sin respuesta" } else { d }
    )
}

/// ¿Hay una versión nueva? `installed` es la versión del producto (la carpeta `versions\<X>`).
pub fn check(installed: &str) -> Result<Option<Novedad>, String> {
    let body = fetch(RELEASES_API, Duration::from_secs(20))?;
    pick(&body, installed)
}

/// Descarga el instalador y su suma a `dir` y comprueba el SHA-256. Devuelve la ruta del instalador.
pub fn download(n: &Novedad, dir: &Path) -> Result<PathBuf, String> {
    std::fs::create_dir_all(dir).map_err(|e| format!("no se pudo crear {}: {e}", dir.display()))?;
    let sums = fetch(&n.sumas_url, Duration::from_secs(30))?;
    let expected = expected_sha256(&String::from_utf8_lossy(&sums), &n.instalador)
        .ok_or_else(|| format!("{SUMS} de la versión {} no trae la suma del instalador", n.version))?;
    let target = dir.join(&n.instalador);
    let partial = dir.join(format!("{}.part", n.instalador));
    let _ = std::fs::remove_file(&partial);
    let out = curl_base(Duration::from_secs(3600))
        .args(["-o"])
        .arg(&partial)
        .arg(&n.instalador_url)
        .output()
        .map_err(|e| format!("no se pudo ejecutar curl: {e}"))?;
    if !out.status.success() {
        let _ = std::fs::remove_file(&partial);
        return Err(sin_conexion(&String::from_utf8_lossy(&out.stderr)));
    }
    let got = sha256_file(&partial).map_err(|e| format!("no se pudo leer la descarga: {e}"))?;
    if got != expected {
        let _ = std::fs::remove_file(&partial);
        return Err("La descarga está dañada o no es la publicada (su SHA-256 no coincide). No se ha instalado nada; \
                    vuelve a intentarlo."
            .into());
    }
    let _ = std::fs::remove_file(&target);
    std::fs::rename(&partial, &target).map_err(|e| format!("no se pudo guardar el instalador: {e}"))?;
    Ok(target)
}

/// Parámetros del instalador para instalar encima sin preguntas (barra de progreso visible).
pub fn installer_args(role: Option<&str>, log: &Path) -> String {
    let mut a = String::from("/SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS");
    if let Some(r) = role.filter(|r| matches!(*r, "control" | "store" | "central" | "viewer")) {
        a.push_str(&format!(" /TYPE={r}"));
    }
    a.push_str(&format!(" \"/LOG={}\"", log.display()));
    a
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rel(tag: &str, pre: bool, with_assets: bool) -> serde_json::Value {
        let v = tag.trim_start_matches('v');
        let dl = format!("https://github.com/mauricioSas/vms-multimarca/releases/download/{tag}");
        let assets = if with_assets {
            serde_json::json!([
                {"name": format!("VMSMultimarca-Setup-{v}.exe"), "browser_download_url": format!("{dl}/VMSMultimarca-Setup-{v}.exe"), "size": 224_000_000},
                {"name": "SHA256SUMS.txt", "browser_download_url": format!("{dl}/SHA256SUMS.txt"), "size": 196}
            ])
        } else {
            serde_json::json!([])
        };
        serde_json::json!({"tag_name": tag, "draft": false, "prerelease": pre,
                           "html_url": format!("https://github.com/mauricioSas/vms-multimarca/releases/tag/{tag}"),
                           "assets": assets})
    }

    #[test]
    fn semver_order_with_prereleases() {
        assert_eq!(compare("2.0.0-beta.2", "2.0.0-beta.1"), Ordering::Greater);
        assert_eq!(compare("2.0.0-beta.10", "2.0.0-beta.9"), Ordering::Greater);
        assert_eq!(compare("2.0.0", "2.0.0-beta.9"), Ordering::Greater);
        assert_eq!(compare("2.0.1", "2.0.0"), Ordering::Greater);
        assert_eq!(compare("v2.0.0-beta.1", "2.0.0-beta.1"), Ordering::Equal);
        assert_eq!(compare("2.0.0-beta.1", "2.0.0-rc.1"), Ordering::Less);
        assert_eq!(compare("2.0.0-ci.45", "2.0.0-beta.1"), Ordering::Greater, "solo orden léxico de etiquetas");
    }

    #[test]
    fn picks_the_newest_published_version_with_installer_and_sums() {
        let list = serde_json::json!([
            rel("v2.0.0-beta.3", true, false), // sin instalador: no vale
            rel("v2.0.0-beta.2", true, true),
            rel("v2.0.0-beta.1", true, true),
        ]);
        let n = pick(list.to_string().as_bytes(), "2.0.0-beta.1").unwrap().unwrap();
        assert_eq!(n.version, "2.0.0-beta.2");
        assert_eq!(n.instalador, "VMSMultimarca-Setup-2.0.0-beta.2.exe");
        assert!(n.instalador_url.ends_with("/v2.0.0-beta.2/VMSMultimarca-Setup-2.0.0-beta.2.exe"));
        assert!(n.notas_url.ends_with("/tag/v2.0.0-beta.2"));
        assert_eq!(pick(list.to_string().as_bytes(), "2.0.0-beta.2").unwrap(), None);
    }

    #[test]
    fn a_final_version_only_gets_final_versions() {
        let list = serde_json::json!([rel("v2.1.0-beta.1", true, true), rel("v2.0.1", false, true)]);
        assert_eq!(pick(list.to_string().as_bytes(), "2.0.0").unwrap().unwrap().version, "2.0.1");
        assert_eq!(pick(list.to_string().as_bytes(), "2.0.0-beta.4").unwrap().unwrap().version, "2.1.0-beta.1");
    }

    #[test]
    fn downloads_only_from_this_repository() {
        let mut r = rel("v2.0.0-beta.2", true, true);
        r["assets"][0]["browser_download_url"] = "https://evil.example/VMSMultimarca-Setup-2.0.0-beta.2.exe".into();
        let list = serde_json::json!([r]);
        assert_eq!(pick(list.to_string().as_bytes(), "2.0.0-beta.1").unwrap(), None);
        let mut bad = rel("v../../x", true, true);
        bad["tag_name"] = "v../../x".into();
        assert_eq!(pick(serde_json::json!([bad]).to_string().as_bytes(), "2.0.0-beta.1").unwrap(), None);
        assert!(pick(b"{no es json", "2.0.0").is_err());
    }

    #[test]
    fn sums_file_lookup_and_file_hash() {
        let hex = "a".repeat(64);
        let sums = format!("{hex}  VMSMultimarca-Setup-2.0.0-beta.2.exe\n{}  sbom.json\n", "b".repeat(64));
        assert_eq!(expected_sha256(&sums, "VMSMultimarca-Setup-2.0.0-beta.2.exe"), Some(hex));
        assert_eq!(expected_sha256(&sums, "otro.exe"), None);
        assert_eq!(expected_sha256("corto  x.exe", "x.exe"), None);
        let d = std::env::temp_dir().join(format!("vms-gh-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let f = d.join("x.bin");
        std::fs::write(&f, b"abc").unwrap();
        assert_eq!(sha256_file(&f).unwrap(), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn installer_runs_silently_with_the_installed_role() {
        let a = installer_args(Some("store"), Path::new("C:\\Temp\\vms update.log"));
        assert!(a.starts_with("/SILENT /SUPPRESSMSGBOXES /NORESTART"));
        assert!(a.contains("/TYPE=store") && a.contains("\"/LOG=C:\\Temp\\vms update.log\""));
        assert!(!installer_args(Some("store\" /EVIL"), Path::new("x")).contains("EVIL"));
        assert!(!installer_args(None, Path::new("x")).contains("/TYPE="));
    }
}
