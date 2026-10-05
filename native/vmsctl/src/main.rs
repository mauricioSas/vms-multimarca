//! `vmsctl.exe`: anfitrión de procesos y configuración del equipo (PLAN-V2 §1.3, CONTRATO §14). Dueño: B1.
//!
//! ```text
//! vmsctl run --service <Servicio> [--exit-on-crash] [--stop-on-stdin-eof]   (lo lanza vmshost)
//! vmsctl services install --role control|store|central|viewer [--data-dir <ruta>] [--recordings-dir <ruta>]
//! vmsctl services uninstall [--purge] | start|stop|restart [--only VMSBackend,...] | status
//! vmsctl firewall apply --profiles private[,domain] | firewall remove
//! vmsctl acl apply [--recordings-dir <ruta>]
//! vmsctl ports check
//! vmsctl health wait [--timeout 120] [--deep]
//! vmsctl version show | switch <X.Y.Z> | confirm | rollback | slot <a|b> | slot-confirm | slot-rollback
//! vmsctl update check | status | rollback [--to <X.Y.Z>] [--reason <texto>]
//! vmsctl tls setup --hostname <nombre> [--ip <IP>]... [--import-root]
//! vmsctl kiosk rotate
//! vmsctl diag bundle --out <zip>
//! vmsctl migrate-from-v1 [--dry-run] [--role <puesto>] [--remove-v1-files] [--profiles private]
//! ```
//! Todas aceptan `--json` (una línea) y `--data-dir`/`--install-dir`. Códigos de salida: CONTRATO §14.2.

mod acl;
mod cli;
mod ctx;
mod diag;
mod envfile;
mod firewall;
mod health;
mod migrate;
mod ports;
mod run;
mod scm;
mod services_cmd;
mod sys;
mod tls;
mod update;
mod version;
#[cfg(windows)]
mod winreg;

use cli::{Args, CtlError, CtlResult, Outcome};
use ctx::Ctx;
use scm::Scm;
use serde_json::json;
use services_cmd::{InstallOpts, Platform};
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Duration;
use sys::RealRunner;
use vms_common::exit_codes;
use vms_common::services::{by_name, Role};

const HELP: &str = "vmsctl: configuración del equipo y anfitrión de los servicios de VMS Multimarca.

  run --service <Servicio>                  anfitrión del proceso (lo usa vmshost)
  services install --role <puesto>          control | store | central | viewer
  services uninstall [--purge]              --purge borra también datos y grabaciones
  services start|stop|restart [--only A,B]  services status
  firewall apply --profiles private[,domain] | firewall remove
  acl apply                                 permisos de la carpeta de datos por SID
  ports check                               puertos libres y fuera de rangos reservados
  health wait [--timeout 120] [--deep]      0 = sano, 12 = no
  version show | switch <X> | confirm | rollback | slot <a|b> | slot-confirm | slot-rollback
  update check | status | rollback [--to <X>]   (consola de Administrador)
  tls setup --hostname <nombre> [--ip <IP>] [--import-root]
  kiosk rotate                              nuevo token de los muros
  diag bundle --out <zip>                   registros y estado, sin secretos
  migrate-from-v1 [--dry-run]               de WinSW (v1) a vmshost, datos intactos

Opciones comunes: --json, --data-dir <ruta>, --install-dir <ruta>.";

/// Interruptores admitidos (una errata como «--purg» no se ignora en silencio).
const KNOWN_SWITCHES: &[&str] = &[
    "--json",
    "--purge",
    "--deep",
    "--dry-run",
    "--remove-v1-files",
    "--import-root",
    "--no-engine-config",
    "--no-acl",
    "--exit-on-crash",
    "--stop-on-stdin-eof",
];

fn real_scm(write: bool) -> Result<Box<dyn Scm>, CtlError> {
    #[cfg(windows)]
    {
        Ok(Box::new(scm::win::WinScm::connect(write)?))
    }
    #[cfg(not(windows))]
    {
        let _ = write;
        Err(CtlError::usage(
            "los servicios de Windows solo existen en Windows (en desarrollo usa vmshost --foreground)",
        ))
    }
}

/// Tipo de puesto: `--role` → el que anotó el instalador en el registro → error.
fn role_of(a: &Args) -> Result<Role, CtlError> {
    if let Some(r) = a.value("--role") {
        return Role::parse(r).ok_or_else(|| {
            CtlError::usage(format!("tipo de puesto desconocido «{r}» (control, store, central o viewer)"))
        });
    }
    #[cfg(windows)]
    if let Ok(Some(r)) = winreg::get_string(winreg::PRODUCT_KEY, "Role") {
        if let Some(role) = Role::parse(&r) {
            return Ok(role);
        }
    }
    Err(CtlError::usage("indica el tipo de puesto con --role (control, store, central o viewer)"))
}

fn timeout_of(a: &Args, default_s: u64) -> Result<Duration, CtlError> {
    match a.value("--timeout") {
        None => Ok(Duration::from_secs(default_s)),
        Some(t) => t
            .parse::<u64>()
            .map(Duration::from_secs)
            .map_err(|_| CtlError::usage(format!("--timeout necesita un número de segundos, no «{t}»"))),
    }
}

fn need<'a>(a: &'a Args, flag: &str, what: &str) -> Result<&'a str, CtlError> {
    a.value(flag).ok_or_else(|| CtlError::usage(format!("falta {flag} {what}")))
}

fn install_opts(a: &Args, ctx: &Ctx, role: Role) -> InstallOpts {
    let _ = ctx;
    InstallOpts {
        role,
        engine_config: !a.has("--no-engine-config"),
        python: a.value("--python").map(PathBuf::from),
        extra_recordings: a.value("--recordings-dir").map(PathBuf::from),
        acl: !a.has("--no-acl"),
    }
}

fn dispatch(a: &Args, ctx: &Ctx) -> CtlResult {
    let w = |i| a.word(i).unwrap_or("");
    match (w(0), w(1)) {
        ("services", "install") => {
            let role = Role::parse(need(a, "--role", "control|store|central|viewer")?)
                .ok_or_else(|| CtlError::usage("tipo de puesto desconocido (control, store, central o viewer)"))?;
            let mut scm = real_scm(true)?;
            services_cmd::install(
                ctx,
                Platform { scm: scm.as_mut(), runner: &mut RealRunner },
                &install_opts(a, ctx, role),
            )
        }
        ("services", "uninstall") => {
            let mut scm = real_scm(true)?;
            services_cmd::uninstall(ctx, Platform { scm: scm.as_mut(), runner: &mut RealRunner }, a.has("--purge"))
        }
        ("services", cmd @ ("start" | "stop" | "restart")) => {
            let only = services_cmd::parse_only(a.value("--only"))?;
            let mut scm = real_scm(false)?;
            let only = only.as_deref();
            if cmd != "start" {
                let out = services_cmd::stop(Platform { scm: scm.as_mut(), runner: &mut RealRunner }, only)?;
                if cmd == "stop" {
                    return Ok(out);
                }
            }
            services_cmd::start(Platform { scm: scm.as_mut(), runner: &mut RealRunner }, only)
        }
        ("services", "status") => {
            let scm = real_scm(false)?;
            let v = services_cmd::summary(scm.as_ref())?;
            let text = v
                .as_array()
                .map(|l| {
                    l.iter()
                        .map(|s| format!("{} {}", s["name"].as_str().unwrap_or("?"), s["state"]))
                        .collect::<Vec<_>>()
                        .join("\n")
                })
                .unwrap_or_default();
            Ok(Outcome::new(json!({"services": v}), text))
        }
        ("firewall", "apply") => {
            let profiles = firewall::parse_profiles(need(a, "--profiles", "private[,domain]")?)?;
            let role = role_of(a)?;
            let net = envfile::NetSettings::load(&ctx.data.env_file());
            let rules = firewall::rules_for(role, &net);
            firewall::apply(&mut RealRunner, &rules, &profiles, &ctx.data.state_dir().join("firewall.json"))?;
            let names: Vec<String> = rules.iter().map(|r| r.name.clone()).collect();
            let text = format!("Reglas del firewall ({profiles}): {}", names.join(", "));
            Ok(Outcome::new(json!({"profiles": profiles, "rules": names}), text))
        }
        ("firewall", "remove") => {
            let removed = firewall::remove(&mut RealRunner, &[], &ctx.data.state_dir().join("firewall.json"))?;
            Ok(Outcome::new(json!({"removed": removed}), format!("Reglas quitadas: {}", removed.len())))
        }
        ("acl", "apply") => {
            let role = role_of(a)?;
            acl::ensure_operators_group()?;
            let install = ctx.install().ok();
            let steps = acl::plan(
                &ctx.data,
                install,
                &role.services(),
                a.value("--recordings-dir").map(std::path::Path::new),
                &|p| p.is_file(),
            );
            let n = acl::apply(&mut RealRunner, &steps)?;
            Ok(Outcome::new(json!({"steps": n, "role": role.as_str()}), format!("Permisos aplicados ({n} pasos).")))
        }
        ("ports", "check") => {
            let role = role_of(a)?;
            ports::check(role, &envfile::NetSettings::load(&ctx.data.env_file()))
        }
        ("health", "wait") => {
            let timeout = timeout_of(a, 120)?;
            let net = envfile::NetSettings::load(&ctx.data.env_file());
            if cfg!(windows) && a.value("--role").is_none() {
                let scm = real_scm(false)?;
                let names: Vec<&str> = services_cmd::installed(scm.as_ref())?.iter().map(|d| d.name).collect();
                if names.is_empty() {
                    return Err(CtlError::health("no hay servicios de VMS Multimarca instalados"));
                }
                health::wait(Some(scm.as_ref()), &names, &net, &ctx.data, timeout, a.has("--deep"))
            } else {
                let names: Vec<&str> = role_of(a)?.services().iter().map(|d| d.name).collect();
                health::wait(None, &names, &net, &ctx.data, timeout, a.has("--deep"))
            }
        }
        ("version", "show") | ("version", "") => version::show(ctx),
        ("version", "switch") => {
            version::switch(ctx, a.word(2).ok_or_else(|| CtlError::usage("uso: version switch <X.Y.Z>"))?)
        }
        ("version", "confirm") => version::confirm(ctx),
        ("version", "rollback") => version::rollback(ctx),
        ("version", "slot") => version::slot(ctx, a.word(2).ok_or_else(|| CtlError::usage("uso: version slot <a|b>"))?),
        ("version", "slot-confirm") => version::slot_confirm(ctx),
        ("version", "slot-rollback") => version::slot_rollback(ctx),
        ("update", "check") => update::check(ctx),
        ("update", "status") => update::status(ctx),
        ("update", "rollback") => update::rollback(ctx, a.value("--to"), a.value("--reason")),
        ("tls", "setup") => {
            let python = ctx.python(a.value("--python"))?;
            tls::setup(
                ctx,
                &mut RealRunner,
                &python,
                need(a, "--hostname", "<nombre del equipo>")?,
                &a.values("--ip"),
                a.has("--import-root"),
            )
        }
        ("kiosk", "rotate") => tls::kiosk_rotate(ctx, &mut RealRunner),
        ("diag", "bundle") => {
            let out = PathBuf::from(need(a, "--out", "<archivo.zip>")?);
            let services = real_scm(false).and_then(|s| services_cmd::summary(s.as_ref())).unwrap_or(json!(null));
            diag::bundle(ctx, &out, services)
        }
        ("migrate-from-v1", _) => {
            let role = match a.value("--role") {
                Some(_) => Some(role_of(a)?),
                None => None,
            };
            let profiles = firewall::parse_profiles(a.value("--profiles").unwrap_or("private"))?;
            let mut scm = real_scm(true)?;
            let opts = migrate::MigrateOpts {
                role,
                dry_run: a.has("--dry-run"),
                remove_v1_files: a.has("--remove-v1-files"),
                profiles,
                install: install_opts(a, ctx, role.unwrap_or(Role::Control)),
            };
            migrate::migrate(ctx, Platform { scm: scm.as_mut(), runner: &mut RealRunner }, opts)
        }
        ("help", _) | ("", _) => Ok(Outcome::new(json!({"help": HELP}), HELP)),
        (cmd, sub) => Err(CtlError::usage(format!("orden desconocida «{cmd} {sub}». Usa «vmsctl help»."))),
    }
}

fn main() -> ExitCode {
    let raw: Vec<String> = std::env::args().skip(1).collect();
    if raw.first().map(String::as_str) == Some("--version") {
        println!("vmsctl {}", env!("CARGO_PKG_VERSION"));
        return ExitCode::SUCCESS;
    }
    let json_mode = raw.iter().any(|a| a == "--json");
    let args = match Args::parse(&raw) {
        Ok(a) => a,
        Err(e) => return ExitCode::from(cli::emit(json_mode, Err(e)) as u8),
    };
    let unknown = args.unknown_switches(KNOWN_SWITCHES);
    if !unknown.is_empty() {
        let e = CtlError::usage(format!("opción desconocida: {}. Usa «vmsctl help».", unknown.join(", ")));
        return ExitCode::from(cli::emit(json_mode, Err(e)) as u8);
    }
    let ctx = Ctx::from_args(&args);
    if args.word(0) == Some("run") {
        return ExitCode::from(run_cmd(&args, &ctx) as u8);
    }
    ExitCode::from(cli::emit(ctx.json, dispatch(&args, &ctx)) as u8)
}

/// `vmsctl run`: no escribe JSON (su salida es el registro del servicio).
fn run_cmd(a: &Args, ctx: &Ctx) -> i32 {
    let Some(name) = a.value("--service") else {
        eprintln!("vmsctl run: falta --service <Servicio>");
        return exit_codes::USAGE;
    };
    let Some(def) = by_name(name) else {
        eprintln!("vmsctl run: servicio desconocido «{name}»");
        return exit_codes::USAGE;
    };
    let mut spec = match run::build_spec(def, ctx, a) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("vmsctl run: {}", e.message);
            let mut log = vms_common::logfile::RotatingLog::standard(ctx.data.logs_dir().join(def.log_name()));
            log.event(&format!("[vmsctl] no se puede arrancar {}: {}", def.name, e.message));
            return e.exit;
        }
    };
    let (tx, rx) = std::sync::mpsc::channel();
    if a.has("--stop-on-stdin-eof") {
        run::stdin_eof_signal(tx);
        run::run(&mut spec, &rx)
    } else {
        // Sin vmshost (consola): se para con Ctrl+C, que cierra el Job Object y con él al proceso.
        let code = run::run(&mut spec, &rx);
        drop(tx);
        code
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unknown_command_is_usage_error() {
        let a = Args::parse(&["frobnicate".to_string()]).unwrap();
        let d = tempfile::tempdir().unwrap();
        let ctx = Ctx::for_tests(d.path(), None, None);
        let e = dispatch(&a, &ctx).err().unwrap();
        assert_eq!(e.exit, exit_codes::USAGE);
        let a = Args::parse(&["ports".into(), "check".into(), "--role".into(), "tienda".into()]).unwrap();
        assert_eq!(dispatch(&a, &ctx).err().unwrap().exit, exit_codes::USAGE);
    }
}
