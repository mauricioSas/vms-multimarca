//! `vmsctl migrate-from-v1`: de los servicios WinSW de la v1 (`install.ps1`) a `vmshost` con cuentas
//! virtuales, **sin tocar los datos** (configuración, contraseñas cifradas y grabaciones se quedan donde
//! están: `%ProgramData%\VMSMultimarca`).
//!
//! Pasos (con `--dry-run` solo se listan):
//! 1. Detecta los servicios de la v1 (`VMSBackend`, `VMSAnalytics`, `VMSHeartbeat`, `VMSCentral` lanzados
//!    por `<instalación>\services\<Servicio>.exe`) y deduce el tipo de puesto.
//! 2. Los para (y cualquier `mediamtx.exe` de la v1 que hubiera quedado huérfano).
//! 3. Quita las reglas del firewall de la v1 («VMS Multimarca - …», por programa).
//! 4. `services install --role <puesto>`: los mismos nombres pasan a `vmshost` y `NT SERVICE\…`; el motor
//!    pasa a ser su propio servicio (`VMSEngine`); ACL por SID (las de la v1 solo dejaban a SYSTEM).
//! 5. Reglas nuevas del firewall (por puerto, perfil privado).
//! 6. Con `--remove-v1-files`, borra el código de la v1 de la carpeta de instalación (nunca los datos).
//!
//! El `secret.key` en claro de la v1 lo cifra con DPAPI el propio backend al arrancar (`vms.core.winsec`).

use crate::cli::{CtlError, Outcome};
use crate::ctx::Ctx;
use crate::envfile::NetSettings;
use crate::firewall;
use crate::scm::SvcState;
use crate::services_cmd::{self, is_ours, InstallOpts, Platform};
use crate::sys::system32;
use serde_json::json;
use std::ffi::OsString;
use std::path::Path;
use std::time::{Duration, Instant};
use vms_common::services::{Role, ANALYTICS, BACKEND, CENTRAL, HEARTBEAT};

pub const V1_SERVICES: [&str; 4] = [ANALYTICS, HEARTBEAT, CENTRAL, BACKEND];

/// Lo que copiaba `install.ps1` en la carpeta de instalación (solo código; los datos están en ProgramData).
pub const V1_PATHS: &[&str] = &[
    "services",
    ".venv",
    "python",
    "downloads",
    "vms",
    "analytics",
    "central",
    "deploy",
    "pyproject.toml",
    ".env.example",
    "LEEME.md",
    "THIRD_PARTY_NOTICES.txt",
    "requirements-vms.txt",
    "requirements-analytics.txt",
    "requirements-central.txt",
    r"bin\mediamtx.exe",
    r"bin\MEDIAMTX-LICENSE.txt",
];

pub fn guess_role(found: &[&str]) -> Role {
    if found.contains(&CENTRAL) && !found.contains(&BACKEND) {
        Role::Central
    } else if found.contains(&ANALYTICS) || found.contains(&HEARTBEAT) {
        Role::Store
    } else {
        Role::Control
    }
}

/// Nombres de las reglas que creaba `install.ps1` (dependen de los puertos del `.env`).
pub fn v1_rule_names(net: &NetSettings) -> Vec<String> {
    let ice = net.ice_udp().map(|(_, p)| p).unwrap_or(8189);
    vec![
        format!("VMS Multimarca - Web (TCP {})", net.http_port()),
        format!("VMS Multimarca - Video WebRTC (UDP {ice})"),
        format!("VMS Multimarca - Video WebRTC (TCP {ice})"),
        format!("VMS Multimarca - Panel central (TCP {})", net.central_port()),
    ]
}

pub struct MigrateOpts {
    pub role: Option<Role>,
    pub dry_run: bool,
    pub remove_v1_files: bool,
    pub profiles: String,
    pub install: InstallOpts,
}

pub fn migrate(ctx: &Ctx, p: Platform<'_>, opts: MigrateOpts) -> Result<Outcome, CtlError> {
    let mut found = Vec::new();
    for name in V1_SERVICES {
        if let Some(info) = p.scm.query(name)? {
            if !is_ours(&info.image_path) {
                found.push(name);
            }
        }
    }
    if found.is_empty() {
        return Ok(Outcome::new(
            json!({"migrated": false, "found": []}),
            "No hay una instalación v1 (servicios con WinSW) en este equipo: nada que migrar.",
        ));
    }
    let role = opts.role.unwrap_or_else(|| guess_role(&found));
    let install = ctx.install()?;
    let net = NetSettings::load(&ctx.data.env_file());
    let old_rules = v1_rule_names(&net);
    let v1_files: Vec<String> = V1_PATHS
        .iter()
        .map(|rel| install.root.join(rel))
        .filter(|p| p.exists())
        .map(|p| p.to_string_lossy().into_owned())
        .collect();
    let plan = json!({
        "found": found, "role": role.as_str(), "stop": found,
        "remove_firewall_rules": old_rules, "install_services": role.services().iter().map(|s| s.name).collect::<Vec<_>>(),
        "v1_files": v1_files, "remove_v1_files": opts.remove_v1_files, "data_dir": ctx.data.root,
    });
    if opts.dry_run {
        return Ok(Outcome::new(
            json!({"migrated": false, "dry_run": true, "plan": plan}),
            format!(
                "Se migraría el puesto «{}» ({}). Repite sin --dry-run para hacerlo.",
                role.as_str(),
                found.join(", ")
            ),
        ));
    }
    for name in &found {
        p.scm.stop(name)?;
    }
    let t0 = Instant::now();
    for name in &found {
        while p.scm.query(name)?.is_some_and(|i| i.state != SvcState::Stopped) {
            if t0.elapsed() > Duration::from_secs(60) {
                return Err(CtlError::windows(format!("el servicio {name} de la v1 no se para")));
            }
            std::thread::sleep(Duration::from_millis(250));
        }
    }
    let killed = kill_v1_engine(&install.root.join("bin"));
    for n in &old_rules {
        let args: Vec<OsString> = ["advfirewall", "firewall", "delete", "rule"]
            .iter()
            .map(OsString::from)
            .chain([OsString::from(format!("name={n}"))])
            .collect();
        // Puede no existir: el código no importa.
        let _ = p.runner.run(&system32("netsh.exe"), &args, &[]);
    }
    let opts_install = InstallOpts { role, ..opts.install };
    let installed = services_cmd::install(ctx, Platform { scm: &mut *p.scm, runner: &mut *p.runner }, &opts_install)?;
    let rules = firewall::rules_for(role, &net);
    firewall::apply(p.runner, &rules, &opts.profiles, &ctx.data.state_dir().join("firewall.json"))?;
    let mut removed_files = Vec::new();
    if opts.remove_v1_files {
        for rel in V1_PATHS {
            let path = install.root.join(rel);
            let res = if path.is_dir() { std::fs::remove_dir_all(&path) } else { std::fs::remove_file(&path) };
            if res.is_ok() {
                removed_files.push(path.to_string_lossy().into_owned());
            }
        }
    }
    Ok(Outcome::new(
        json!({"migrated": true, "plan": plan, "install": installed.data, "killed_engines": killed,
               "firewall_rules": rules.iter().map(|r| r.name.clone()).collect::<Vec<_>>(), "removed_files": removed_files}),
        format!(
            "Migrado de la v1 al puesto «{}»: servicios con vmshost y cuentas virtuales; datos y grabaciones intactos. \
             Arráncalos con «vmsctl services start».",
            role.as_str()
        ),
    ))
}

/// Mata los `mediamtx.exe` que se ejecuten desde `dir` (motor de la v1 huérfano). Devuelve cuántos.
#[cfg(windows)]
fn kill_v1_engine(dir: &Path) -> u32 {
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W, TH32CS_SNAPPROCESS,
    };
    use windows_sys::Win32::System::Threading::{
        OpenProcess, QueryFullProcessImageNameW, TerminateProcess, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_TERMINATE,
    };
    let want = dir.join("mediamtx.exe").to_string_lossy().to_ascii_lowercase();
    let mut killed = 0;
    // SAFETY: instantánea de procesos recorrida con estructuras propias; handles cerrados.
    unsafe {
        let snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if snap == INVALID_HANDLE_VALUE {
            return 0;
        }
        let mut e: PROCESSENTRY32W = std::mem::zeroed();
        e.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
        let mut ok = Process32FirstW(snap, &mut e);
        while ok != 0 {
            let len = e.szExeFile.iter().position(|c| *c == 0).unwrap_or(e.szExeFile.len());
            if String::from_utf16_lossy(&e.szExeFile[..len]).eq_ignore_ascii_case("mediamtx.exe") {
                let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE, 0, e.th32ProcessID);
                if !h.is_null() {
                    let mut buf = [0u16; 1024];
                    let mut size = buf.len() as u32;
                    if QueryFullProcessImageNameW(h, 0, buf.as_mut_ptr(), &mut size) != 0 {
                        let path = String::from_utf16_lossy(&buf[..size as usize]).to_ascii_lowercase();
                        if path == want && TerminateProcess(h, 1) != 0 {
                            killed += 1;
                        }
                    }
                    CloseHandle(h);
                }
            }
            ok = Process32NextW(snap, &mut e);
        }
        CloseHandle(snap);
    }
    killed
}

#[cfg(not(windows))]
fn kill_v1_engine(_dir: &Path) -> u32 {
    0
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scm::fake::FakeScm;
    use crate::sys::fake::FakeRunner;
    use vms_common::layout::{Component, InstallLayout};

    fn setup() -> (tempfile::TempDir, Ctx, InstallLayout) {
        let d = tempfile::tempdir().unwrap();
        let inst = InstallLayout::new(d.path().join("pf"));
        std::fs::create_dir_all(inst.bin_dir()).unwrap();
        std::fs::write(inst.vmshost_exe(), b"").unwrap();
        std::fs::create_dir_all(inst.version_vmsctl("2.0.0").parent().unwrap()).unwrap();
        std::fs::write(inst.version_vmsctl("2.0.0"), b"").unwrap();
        std::fs::create_dir_all(inst.root.join("services")).unwrap();
        std::fs::write(inst.root.join("pyproject.toml"), b"").unwrap();
        let comp = Component::Version { version: "2.0.0".into(), root: inst.version_dir("2.0.0") };
        let ctx = Ctx::for_tests(&d.path().join("pd"), Some(&inst.root), Some(comp));
        // Datos de la v1 que deben quedar intactos
        std::fs::create_dir_all(ctx.data.recordings_dir().join("cam-1")).unwrap();
        std::fs::write(ctx.data.recordings_dir().join("cam-1").join("seg.mp4"), b"video").unwrap();
        std::fs::create_dir_all(ctx.data.config_dir()).unwrap();
        std::fs::write(ctx.data.config_dir().join("config.json"), b"{\"version\":1}").unwrap();
        std::fs::write(ctx.data.env_file(), "VMS_HTTP_PORT=8601\n").unwrap();
        (d, ctx, inst)
    }

    fn v1_scm(inst: &InstallLayout, names: &[&str]) -> FakeScm {
        let mut scm = FakeScm::default();
        for n in names {
            let img = format!("\"{}\"", inst.root.join("services").join(format!("{n}.exe")).display());
            scm.foreign.insert(n.to_string(), (img, "LocalSystem".into(), SvcState::Running));
        }
        scm
    }

    fn opts(dry_run: bool, remove: bool) -> MigrateOpts {
        MigrateOpts {
            role: None,
            dry_run,
            remove_v1_files: remove,
            profiles: "private".into(),
            install: InstallOpts {
                role: Role::Control,
                engine_config: true,
                python: Some("/py".into()),
                extra_recordings: None,
                acl: true,
            },
        }
    }

    #[test]
    fn nothing_to_migrate_without_winsw_services() {
        let (_d, ctx, _) = setup();
        let mut scm = FakeScm::default();
        let mut r = FakeRunner::default();
        let out = migrate(&ctx, Platform { scm: &mut scm, runner: &mut r }, opts(false, false)).unwrap();
        assert_eq!(out.data["migrated"], false);
        assert!(r.calls.is_empty());
    }

    #[test]
    fn dry_run_lists_the_plan_and_changes_nothing() {
        let (_d, ctx, inst) = setup();
        let mut scm = v1_scm(&inst, &[BACKEND, ANALYTICS, HEARTBEAT]);
        let mut r = FakeRunner::default();
        let out = migrate(&ctx, Platform { scm: &mut scm, runner: &mut r }, opts(true, false)).unwrap();
        assert_eq!(out.data["plan"]["role"], "store");
        assert!(out.data["plan"]["remove_firewall_rules"][0].as_str().unwrap().contains("TCP 8601"));
        assert!(r.calls.is_empty() && scm.ops.is_empty());
    }

    #[test]
    fn migrates_winsw_services_to_vmshost_and_keeps_data() {
        let (_d, ctx, inst) = setup();
        let mut scm = v1_scm(&inst, &[BACKEND, ANALYTICS, HEARTBEAT]);
        let mut r = FakeRunner { codes: vec![("show rule".into(), 1)], ..Default::default() };
        let out = migrate(&ctx, Platform { scm: &mut scm, runner: &mut r }, opts(false, true)).unwrap();
        assert_eq!(out.data["migrated"], true);
        assert_eq!(scm.ops[..3], ["stop VMSAnalytics", "stop VMSHeartbeat", "stop VMSBackend"]);
        for n in [BACKEND, ANALYTICS, HEARTBEAT, "VMSEngine", "VMSUpdater"] {
            let (spec, _) = &scm.services[n];
            assert_eq!(spec.image, inst.vmshost_exe(), "{n}");
        }
        assert_eq!(scm.services[BACKEND].0.account, "NT SERVICE\\VMSBackend");
        assert!(r.calls.iter().any(|c| c.contains("delete rule name=VMS Multimarca - Web (TCP 8601)")));
        assert!(r.calls.iter().any(|c| c.contains("add rule name=VMSMultimarca-Web-TCP-8601")));
        // Datos intactos; código de la v1 borrado
        assert_eq!(std::fs::read(ctx.data.recordings_dir().join("cam-1").join("seg.mp4")).unwrap(), b"video");
        assert!(ctx.data.config_dir().join("config.json").exists());
        assert!(!inst.root.join("services").exists() && !inst.root.join("pyproject.toml").exists());
        assert!(inst.vmshost_exe().exists() && inst.version_vmsctl("2.0.0").exists());
        // Una segunda ejecución ya no encuentra nada
        let again = migrate(&ctx, Platform { scm: &mut scm, runner: &mut r }, opts(false, false)).unwrap();
        assert_eq!(again.data["migrated"], false);
    }

    #[test]
    fn roles_from_v1_components() {
        assert_eq!(guess_role(&[BACKEND]), Role::Control);
        assert_eq!(guess_role(&[BACKEND, ANALYTICS]), Role::Store);
        assert_eq!(guess_role(&[CENTRAL]), Role::Central);
    }
}
