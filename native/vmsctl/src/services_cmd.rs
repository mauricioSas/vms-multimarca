//! `vmsctl services install|uninstall|start|stop|restart` (CONTRATO §14.1). Sustituye a `install.ps1`
//! y a WinSW: el SCM lanza siempre `"<instalación>\bin\vmshost.exe" service --name <Servicio>` con la
//! cuenta virtual `NT SERVICE\<Servicio>` (LocalSystem solo para `VMSUpdater`).

use crate::acl;
use crate::cli::{CtlError, Outcome};
use crate::ctx::Ctx;
use crate::scm::{Scm, SvcSpec, SvcState};
use crate::sys::Runner;
use serde_json::{json, Value};
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use vms_common::layout::{DataLayout, InstallLayout};
use vms_common::services::{by_name, Role, ServiceDef, BACKEND, ENGINE, SERVICES};
use vms_common::state::Pointer;

pub struct Platform<'a> {
    pub scm: &'a mut dyn Scm,
    pub runner: &'a mut dyn Runner,
}

pub fn spec_for(def: &ServiceDef, install: &InstallLayout, data: &DataLayout) -> SvcSpec {
    SvcSpec {
        name: def.name.to_string(),
        display: def.display.to_string(),
        description: def.description.to_string(),
        image: install.vmshost_exe(),
        args: vec!["service".into(), "--name".into(), def.name.into()],
        account: def.account_name().unwrap_or_else(|| "LocalSystem".to_string()),
        delayed: def.delayed_start,
        env: vec![("VMS_DATA_DIR".into(), data.root.to_string_lossy().into_owned())],
    }
}

/// ¿El servicio es nuestro (lo lanza `vmshost`)? Los de la v1 los lanzaba WinSW.
pub fn is_ours(image_path: &str) -> bool {
    image_path.to_ascii_lowercase().contains("vmshost")
}

/// Servicios nuestros instalados, en orden de arranque.
pub fn installed(scm: &dyn Scm) -> Result<Vec<&'static ServiceDef>, CtlError> {
    let mut out = Vec::new();
    for def in SERVICES.iter() {
        if let Some(info) = scm.query(def.name)? {
            if is_ours(&info.image_path) {
                out.push(def);
            }
        }
    }
    Ok(out)
}

pub fn parse_only(only: Option<&str>) -> Result<Option<Vec<&'static str>>, CtlError> {
    let Some(list) = only else { return Ok(None) };
    let mut out = Vec::new();
    for n in list.split(',').map(str::trim).filter(|n| !n.is_empty()) {
        let def = by_name(n).ok_or_else(|| CtlError::usage(format!("servicio desconocido «{n}»")))?;
        out.push(def.name);
    }
    Ok(Some(out))
}

fn wait_state(scm: &dyn Scm, name: &str, want: SvcState, timeout: Duration) -> Result<(), CtlError> {
    let t0 = Instant::now();
    loop {
        let st = scm.query(name)?.map(|i| i.state);
        if st == Some(want) || (want == SvcState::Stopped && st.is_none()) {
            return Ok(());
        }
        if t0.elapsed() >= timeout {
            return Err(CtlError::windows(format!(
                "{name} no llegó a {want:?} en {} s (está en {st:?})",
                timeout.as_secs()
            )));
        }
        std::thread::sleep(Duration::from_millis(250));
    }
}

pub struct InstallOpts {
    pub role: Role,
    pub engine_config: bool,
    pub python: Option<PathBuf>,
    pub extra_recordings: Option<PathBuf>,
    pub acl: bool,
}

/// Escribe el puntero con la versión de este `vmsctl` (el instalador manda: no queda «a prueba»).
pub fn point_to(data: &DataLayout, install: &InstallLayout, version: &str) -> Result<Pointer, CtlError> {
    let st = data.state();
    let old = st.resolve(|v| install.version_installed(v)).ok().map(|r| r.pointer);
    let mut p = old.clone().unwrap_or_else(|| Pointer::new(version));
    if p.active != version {
        p.previous = Some(p.active.clone());
        p.active = version.to_string();
    }
    p.trial = false;
    p.trial_since_unix = None;
    let p = st.write_pointer(&p)?;
    st.set_last_good(version)?;
    Ok(p)
}

/// `python -m vms engine-config`: escribe `mediamtx.yml` (y el token interno si falta) antes de arrancar.
fn engine_config(runner: &mut dyn Runner, python: &Path, data: &DataLayout) -> Result<(), CtlError> {
    let env = vec![
        ("VMS_DATA_DIR".to_string(), data.root.clone().into_os_string()),
        ("PYTHONDONTWRITEBYTECODE".to_string(), OsString::from("1")),
        ("PYTHONUTF8".to_string(), OsString::from("1")),
        ("VMS_ENGINE_MODE".to_string(), OsString::from("attach")),
    ];
    let args: Vec<OsString> = ["-m", "vms", "engine-config"].iter().map(OsString::from).collect();
    let r = runner.run(python, &args, &env).map_err(|e| CtlError::io(&e, &python.display().to_string()))?;
    if !r.ok() {
        return Err(CtlError::windows(format!(
            "no se pudo generar la configuración del motor (python -m vms engine-config, código {}): {}",
            r.code, r.stderr_tail
        )));
    }
    Ok(())
}

/// Token interno (backend ↔ analítica ↔ latido). Normalmente lo crea `engine-config`; si no, aquí, con el
/// mismo formato que `secrets.token_urlsafe(32)`.
fn ensure_internal_token(data: &DataLayout) -> Result<bool, CtlError> {
    let p = data.secrets_dir().join("internal.token");
    if p.is_file() {
        return Ok(false);
    }
    let bytes = vms_common::secret::random_bytes(32).map_err(|e| CtlError::io(&e, "generador aleatorio"))?;
    vms_common::atomic_write(&p, vms_common::secret::b64url(&bytes).as_bytes())
        .map_err(|e| CtlError::io(&e, &p.display().to_string()))?;
    Ok(true)
}

pub fn install(ctx: &Ctx, p: Platform<'_>, opts: &InstallOpts) -> Result<Outcome, CtlError> {
    let install = ctx.install()?;
    let version = ctx.own_version().ok_or_else(|| {
        CtlError::usage("«services install» se ejecuta con el vmsctl de la versión que se instala (versions\\<X>\\bin)")
    })?;
    if !install.vmshost_exe().is_file() {
        return Err(CtlError::usage(format!("falta {}: copia primero el arrancador", install.vmshost_exe().display())));
    }
    let data = &ctx.data;
    let mut dirs = data.all_dirs();
    if opts.role.has(vms_common::services::CENTRAL) {
        dirs.push(data.central_dir());
    }
    for d in &dirs {
        std::fs::create_dir_all(d).map_err(|e| CtlError::io(&e, &d.display().to_string()))?;
    }
    let pointer = point_to(data, install, version)?;
    let needs_engine = opts.role.has(ENGINE) || opts.role.has(BACKEND);
    // Los servicios corren con cuentas virtuales sin sesión: las contraseñas de los equipos van en el
    // almacén cifrado en archivo (secret.key con DPAPI), el mismo que lee `engine-config` como administrador.
    let env_file = data.env_file();
    let mut env_defaults = vec![("VMS_CREDENTIAL_BACKEND", "file")];
    if needs_engine {
        env_defaults.push(("VMS_ENGINE_MODE", "attach"));
    }
    for (k, v) in env_defaults {
        crate::envfile::set_default(&env_file, k, v).map_err(|e| CtlError::io(&e, &env_file.display().to_string()))?;
    }
    if needs_engine && opts.engine_config {
        let python = match &opts.python {
            Some(p) => p.clone(),
            None => ctx.python(None)?,
        };
        engine_config(p.runner, &python, data)?;
    }
    let token_created = if needs_engine { ensure_internal_token(data)? } else { false };
    let services = opts.role.services();
    let mut out = Vec::new();
    for def in &services {
        let spec = spec_for(def, install, data);
        let created = p.scm.install(&spec)?;
        out.push(json!({"name": def.name, "created": created, "account": spec.account,
                        "image": format!("\"{}\" {}", spec.image.display(), spec.args.join(" "))}));
    }
    // ACL después de crear los servicios: Windows solo acepta el SID `NT SERVICE\…` de un servicio que existe
    // (icacls responde 1332 «No mapping between account names and security IDs» si aún no está).
    let mut acl_steps = 0;
    if opts.acl {
        acl::ensure_operators_group()?;
        acl::precreate_logs(data, &services).map_err(|e| CtlError::io(&e, &data.logs_dir().display().to_string()))?;
        let inv = acl::Inventory::scan(data);
        let steps = acl::plan(data, Some(install), &services, opts.extra_recordings.as_deref(), &inv);
        acl_steps = acl::apply(p.runner, &steps)?;
    }
    // Servicios nuestros que ya no tocan en este puesto (cambio de tipo de puesto): fuera.
    let mut removed = Vec::new();
    for def in SERVICES.iter().rev() {
        if opts.role.has(def.name) {
            continue;
        }
        if let Some(info) = p.scm.query(def.name)? {
            if is_ours(&info.image_path) {
                p.scm.stop(def.name)?;
                wait_state(p.scm, def.name, SvcState::Stopped, Duration::from_secs(60))?;
                p.scm.delete(def.name)?;
                removed.push(def.name);
            }
        }
    }
    #[cfg(windows)]
    {
        use crate::winreg::{set_string, PRODUCT_KEY};
        for (k, v) in [
            ("InstallDir", install.root.to_string_lossy().into_owned()),
            ("DataDir", data.root.to_string_lossy().into_owned()),
            ("Role", opts.role.as_str().to_string()),
            ("InstalledVersion", version.to_string()),
        ] {
            set_string(PRODUCT_KEY, k, &v).map_err(|e| CtlError::io(&e, "registro HKLM\\SOFTWARE\\VMSMultimarca"))?;
        }
    }
    let data_json = json!({
        "role": opts.role.as_str(), "version": version, "pointer": pointer,
        "data_dir": data.root, "install_dir": install.root, "services": out, "removed": removed,
        "acl_steps": acl_steps, "internal_token_created": token_created,
    });
    let text = format!(
        "Servicios del puesto «{}» instalados ({}) con la versión {version}. Arráncalos con «vmsctl services start».",
        opts.role.as_str(),
        services.iter().map(|s| s.name).collect::<Vec<_>>().join(", ")
    );
    Ok(Outcome::new(data_json, text))
}

pub fn start(p: Platform<'_>, only: Option<&[&str]>) -> Result<Outcome, CtlError> {
    let list: Vec<&ServiceDef> =
        installed(p.scm)?.into_iter().filter(|d| only.is_none_or(|o| o.contains(&d.name))).collect();
    if list.is_empty() {
        return Err(CtlError::usage("no hay servicios de VMS Multimarca instalados que arrancar"));
    }
    for def in &list {
        p.scm.start(def.name)?;
    }
    for def in &list {
        wait_state(p.scm, def.name, SvcState::Running, Duration::from_secs(30))?;
    }
    let names: Vec<&str> = list.iter().map(|d| d.name).collect();
    Ok(Outcome::new(json!({"started": names}), format!("En marcha: {}", names.join(", "))))
}

pub fn stop(p: Platform<'_>, only: Option<&[&str]>) -> Result<Outcome, CtlError> {
    let list: Vec<&ServiceDef> =
        installed(p.scm)?.into_iter().rev().filter(|d| only.is_none_or(|o| o.contains(&d.name))).collect();
    for def in &list {
        p.scm.stop(def.name)?;
        wait_state(p.scm, def.name, SvcState::Stopped, Duration::from_secs(60))?;
    }
    let names: Vec<&str> = list.iter().map(|d| d.name).collect();
    Ok(Outcome::new(json!({"stopped": names}), format!("Parados: {}", names.join(", "))))
}

/// Nombre por defecto de la carpeta de datos (`%ProgramData%\VMSMultimarca`).
const DATA_DIR_NAME: &str = "VMSMultimarca";

/// ¿Se puede borrar `root` entera con `--purge`? Solo si es **nuestra**: se llama `VMSMultimarca` o es la
/// `DataDir` que anotó el instalador (`registered`), y además tiene una marca de la instalación
/// (`state\active.json`, o un `.env` con claves `VMS_`). Un `VMS_DATA_DIR` mal puesto (`C:\Users`,
/// `D:\Datos`…) nunca se borra.
pub fn purge_guard(root: &Path, registered: Option<&Path>) -> Result<(), CtlError> {
    let refuse = |why: &str| {
        Err(CtlError::usage(format!(
            "no se borra {}: {why}. Si de verdad es la carpeta de datos de VMS Multimarca, bórrala a mano",
            root.display()
        )))
    };
    if root.components().count() < 3 || root.parent().is_none_or(|p| p.parent().is_none()) {
        return refuse("la ruta es demasiado corta");
    }
    let same = |a: &Path, b: &Path| {
        let norm = |p: &Path| p.to_string_lossy().trim_end_matches(['\\', '/']).to_ascii_lowercase();
        norm(a) == norm(b)
    };
    let named = root.file_name().is_some_and(|n| n.to_string_lossy().eq_ignore_ascii_case(DATA_DIR_NAME));
    if !named && !registered.is_some_and(|r| same(r, root)) {
        return refuse("no se llama VMSMultimarca ni es la carpeta de datos que anotó el instalador");
    }
    let pointer = DataLayout::new(root).state().pointer_path().is_file();
    let env = crate::envfile::read(&root.join(".env")).keys().any(|k| k.starts_with("VMS_"));
    if !pointer && !env {
        return refuse("no tiene state\\active.json ni un .env de VMS Multimarca");
    }
    Ok(())
}

/// Quita los servicios (y las reglas del firewall). Con `purge`, borra también la carpeta de datos.
pub fn uninstall(ctx: &Ctx, p: Platform<'_>, purge: bool) -> Result<Outcome, CtlError> {
    // La DataDir anotada se lee antes de borrar la clave del registro.
    #[cfg(windows)]
    let registered: Option<PathBuf> = crate::winreg::get_string(crate::winreg::PRODUCT_KEY, "DataDir")
        .ok()
        .flatten()
        .filter(|s| !s.is_empty())
        .map(PathBuf::from);
    #[cfg(not(windows))]
    let registered: Option<PathBuf> = None;
    if purge && ctx.data.root.exists() {
        purge_guard(&ctx.data.root, registered.as_deref())?; // antes de tocar nada
    }
    let list = installed(p.scm)?;
    let mut removed = Vec::new();
    for def in list.iter().rev() {
        p.scm.stop(def.name)?;
        wait_state(p.scm, def.name, SvcState::Stopped, Duration::from_secs(60))?;
        p.scm.delete(def.name)?;
        removed.push(def.name);
    }
    let rules = crate::firewall::remove(p.runner, &[], &ctx.data.state_dir().join("firewall.json"))?;
    #[cfg(windows)]
    crate::winreg::delete_tree(crate::winreg::PRODUCT_KEY)
        .map_err(|e| CtlError::io(&e, "registro HKLM\\SOFTWARE\\VMSMultimarca"))?;
    let mut purged = false;
    if purge {
        let root = &ctx.data.root;
        if root.exists() {
            std::fs::remove_dir_all(root).map_err(|e| CtlError::io(&e, &root.display().to_string()))?;
        }
        purged = true;
    }
    let data =
        json!({"removed": removed, "firewall_rules_removed": rules, "purged": purged, "data_dir": ctx.data.root});
    let mut text =
        format!("Servicios quitados: {}.", if removed.is_empty() { "ninguno".into() } else { removed.join(", ") });
    text.push_str(if purged { " Datos y grabaciones borrados." } else { " Los datos y las grabaciones se conservan." });
    Ok(Outcome::new(data, text))
}

pub fn summary(scm: &dyn Scm) -> Result<Value, CtlError> {
    let mut v = Vec::new();
    for def in SERVICES.iter() {
        if let Some(info) = scm.query(def.name)? {
            v.push(
                json!({"name": def.name, "state": info.state, "image_path": info.image_path, "account": info.account}),
            );
        }
    }
    Ok(Value::Array(v))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scm::fake::FakeScm;
    use crate::sys::fake::FakeRunner;
    use vms_common::layout::Component;

    pub struct Fixture {
        pub _d: tempfile::TempDir,
        pub ctx: Ctx,
        pub install: InstallLayout,
    }

    pub fn fixture(version: &str) -> Fixture {
        let d = tempfile::tempdir().unwrap();
        let install = InstallLayout::new(d.path().join("pf"));
        std::fs::create_dir_all(install.bin_dir()).unwrap();
        std::fs::write(install.vmshost_exe(), b"").unwrap();
        std::fs::create_dir_all(install.version_vmsctl(version).parent().unwrap()).unwrap();
        std::fs::write(install.version_vmsctl(version), b"").unwrap();
        let comp = Component::Version { version: version.into(), root: install.version_dir(version) };
        let ctx = Ctx::for_tests(&d.path().join(DATA_DIR_NAME), Some(&install.root), Some(comp));
        Fixture { _d: d, ctx, install }
    }

    fn opts(role: Role) -> InstallOpts {
        InstallOpts { role, engine_config: true, python: Some("/py".into()), extra_recordings: None, acl: true }
    }

    #[test]
    fn install_store_creates_services_with_virtual_accounts_and_vmshost() {
        let f = fixture("2.0.0");
        let mut scm = FakeScm::default();
        let mut runner = FakeRunner::default();
        let out = install(&f.ctx, Platform { scm: &mut scm, runner: &mut runner }, &opts(Role::Store)).unwrap();
        assert_eq!(out.data["services"].as_array().unwrap().len(), 5);
        let (spec, _) = &scm.services["VMSBackend"];
        assert_eq!(spec.account, "NT SERVICE\\VMSBackend");
        assert_eq!(spec.image, f.install.vmshost_exe());
        assert_eq!(spec.args, ["service", "--name", "VMSBackend"]);
        assert!(spec.delayed);
        assert_eq!(spec.env[0].0, "VMS_DATA_DIR");
        assert_eq!(scm.services["VMSUpdater"].0.account, "LocalSystem");
        assert!(!scm.services["VMSEngine"].0.delayed);
        // Orden: engine-config → servicios → ACL (el SID de un servicio solo existe cuando existe el servicio)
        assert!(runner.calls[0].contains("-m vms engine-config"), "{:?}", runner.calls);
        assert!(runner.calls[1].starts_with("icacls.exe"));
        // Puntero y diario con la versión instalada
        let st = f.ctx.data.state();
        let p = st.read_pointer().unwrap();
        assert_eq!((p.active.as_str(), p.trial), ("2.0.0", false));
        assert_eq!(st.last_good().unwrap(), "2.0.0");
        assert!(f.ctx.data.secrets_dir().join("internal.token").is_file());
        let env = crate::envfile::read(&f.ctx.data.env_file());
        assert_eq!((env["VMS_CREDENTIAL_BACKEND"].as_str(), env["VMS_ENGINE_MODE"].as_str()), ("file", "attach"));
        // Idempotente: reinstalar no crea de nuevo
        let out = install(&f.ctx, Platform { scm: &mut scm, runner: &mut runner }, &opts(Role::Store)).unwrap();
        assert!(out.data["services"].as_array().unwrap().iter().all(|s| s["created"] == false));
    }

    #[test]
    fn changing_role_removes_services_that_no_longer_belong() {
        let f = fixture("2.0.0");
        let mut scm = FakeScm::default();
        let mut runner = FakeRunner::default();
        install(&f.ctx, Platform { scm: &mut scm, runner: &mut runner }, &opts(Role::Store)).unwrap();
        let out = install(&f.ctx, Platform { scm: &mut scm, runner: &mut runner }, &opts(Role::Control)).unwrap();
        assert_eq!(out.data["removed"], json!(["VMSHeartbeat", "VMSAnalytics"]));
        assert_eq!(scm.services.keys().cloned().collect::<Vec<_>>(), ["VMSBackend", "VMSEngine", "VMSUpdater"]);
    }

    #[test]
    fn failed_engine_config_stops_the_install_before_touching_services() {
        let f = fixture("2.0.0");
        let mut scm = FakeScm::default();
        let mut runner = FakeRunner { codes: vec![("engine-config".into(), 1)], ..Default::default() };
        let e = install(&f.ctx, Platform { scm: &mut scm, runner: &mut runner }, &opts(Role::Control)).unwrap_err();
        assert!(e.message.contains("engine-config"));
        assert!(scm.services.is_empty());
    }

    #[test]
    fn install_points_to_its_own_version_and_keeps_the_previous() {
        let f = fixture("2.1.0");
        std::fs::create_dir_all(f.install.version_vmsctl("2.0.0").parent().unwrap()).unwrap();
        std::fs::write(f.install.version_vmsctl("2.0.0"), b"").unwrap();
        f.ctx.data.state().write_pointer(&Pointer::new("2.0.0").switched("2.0.0", 1)).unwrap();
        let p = point_to(&f.ctx.data, &f.install, "2.1.0").unwrap();
        assert_eq!((p.active.as_str(), p.previous.as_deref(), p.trial), ("2.1.0", Some("2.0.0"), false));
    }

    #[test]
    fn start_stop_in_order_and_uninstall_keeps_data_unless_purge() {
        let f = fixture("2.0.0");
        let mut scm = FakeScm::default();
        let mut runner = FakeRunner { codes: vec![("show rule".into(), 1)], ..Default::default() };
        install(&f.ctx, Platform { scm: &mut scm, runner: &mut runner }, &opts(Role::Control)).unwrap();
        scm.ops.clear();
        start(Platform { scm: &mut scm, runner: &mut runner }, None).unwrap();
        assert_eq!(scm.ops, ["start VMSEngine", "start VMSBackend", "start VMSUpdater"]);
        scm.ops.clear();
        stop(Platform { scm: &mut scm, runner: &mut runner }, Some(&["VMSBackend"])).unwrap();
        assert_eq!(scm.ops, ["stop VMSBackend"]);
        let out = uninstall(&f.ctx, Platform { scm: &mut scm, runner: &mut runner }, false).unwrap();
        assert_eq!(out.data["removed"], json!(["VMSUpdater", "VMSBackend", "VMSEngine"]));
        assert!(scm.services.is_empty());
        assert!(f.ctx.data.root.exists());
        uninstall(&f.ctx, Platform { scm: &mut scm, runner: &mut runner }, true).unwrap();
        assert!(!f.ctx.data.root.exists());
    }

    #[test]
    fn purge_refuses_folders_that_are_not_ours() {
        // Regresión: la única protección era «3 componentes o más» (C:\Users o D:\Datos pasaban).
        let d = tempfile::tempdir().unwrap();
        let users = d.path().join("Users");
        std::fs::create_dir_all(users.join("ana").join("Documentos")).unwrap();
        std::fs::write(users.join("ana").join("Documentos").join("tesis.docx"), b"no tocar").unwrap();
        let e = purge_guard(&users, None).unwrap_err();
        assert!(e.message.contains("no se llama VMSMultimarca"), "{}", e.message);
        // Aunque tenga un .env cualquiera, si no se llama así ni es la del registro, no
        std::fs::write(users.join(".env"), "VMS_HTTP_PORT=1\n").unwrap();
        assert!(purge_guard(&users, None).is_err());
        // Se llama VMSMultimarca pero no tiene ninguna marca nuestra
        let fake = d.path().join("Datos").join("VMSMultimarca");
        std::fs::create_dir_all(&fake).unwrap();
        assert!(purge_guard(&fake, None).unwrap_err().message.contains("active.json"));
        // La carpeta de datos personalizada que anotó el instalador, con su .env: sí
        let custom = d.path().join("CCTV-datos");
        std::fs::create_dir_all(&custom).unwrap();
        std::fs::write(custom.join(".env"), "VMS_CREDENTIAL_BACKEND=file\n").unwrap();
        assert!(purge_guard(&custom, Some(&custom)).is_ok());
        assert!(purge_guard(&custom, None).is_err());
        // Rutas cortas, nunca
        assert!(purge_guard(Path::new("/VMSMultimarca"), None).is_err());

        // Y uninstall --purge con un VMS_DATA_DIR ajeno no borra nada (ni siquiera quita servicios)
        let f = fixture("2.0.0");
        let mut scm = FakeScm::default();
        let mut runner = FakeRunner { codes: vec![("show rule".into(), 1)], ..Default::default() };
        install(&f.ctx, Platform { scm: &mut scm, runner: &mut runner }, &opts(Role::Control)).unwrap();
        let alien = Ctx::for_tests(&users, Some(&f.install.root), None);
        assert!(uninstall(&alien, Platform { scm: &mut scm, runner: &mut runner }, true).is_err());
        assert!(users.join("ana").join("Documentos").join("tesis.docx").is_file());
        assert_eq!(scm.services.len(), 3, "se negó antes de tocar nada");
    }

    #[test]
    fn only_accepts_known_services() {
        assert_eq!(parse_only(Some("vmsbackend, VMSEngine")).unwrap().unwrap(), ["VMSBackend", "VMSEngine"]);
        assert!(parse_only(Some("Spooler")).is_err());
        assert!(parse_only(None).unwrap().is_none());
    }
}
