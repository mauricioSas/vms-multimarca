//! `vmsctl run --service <S>`: anfitrión del proceso real de cada servicio (CONTRATO §14.1).
//!
//! - Lanza `runtime\python.exe -m <módulo>` o `engine\mediamtx.exe <datos>\mediamtx\mediamtx.yml` dentro de
//!   su propio Job Object (creado suspendido): si `vmsctl` muere, muere el proceso.
//! - Copia su salida, línea a línea y **sin credenciales**, a `logs\<servicio>.log` (MediaMTX en
//!   `logs\engine.log`, que el backend sigue con `vms/engine/logtail.py`), con rotación 10 × 10 MB.
//!   `engine.log` rota copiando y vaciando (nunca se borra ni se recrea: ver `vms_common::logfile`).
//! - Parada en 3 escalones: cierra la entrada estándar del proceso (el backend la vigila con
//!   `VMS_STOP_ON_STDIN_EOF=1`; en POSIX además SIGTERM) → espera `grace` (10 s; 0 para MediaMTX, que no
//!   necesita parada ordenada) → `TerminateJobObject`.
//! - Si el proceso cae, lo relanza con espera creciente (1, 2, 5, 10, 30 s). Con `--exit-on-crash`
//!   (versión a prueba) sale con el código 13 para que `vmshost` cuente la caída.
//! - Publica su estado real en `logs\status-<Servicio>.json` (`crate::runstatus`): es lo que mira
//!   `vmsctl health wait`, porque el SCM solo ve a `vmshost`.
//! - `VMSHeartbeat` sin `VMS_CENTRAL_URL` (sede sin panel central) no se lanza: queda «en espera» (sano) y
//!   arranca en cuanto se configure, en vez de caer y relanzarse sin fin.
//! - Para cuando `vmshost` cierra su entrada estándar (`--stop-on-stdin-eof`).
//! - `VMSUpdater` no lee el `.env` (solo su entorno): `vmsctl` le pasa las variables `VMS_*` del `.env`
//!   (`VMS_UPDATE_SOURCE`, `VMS_HTTP_PORT`…). Las del entorno del proceso ganan. Los servicios que leen el
//!   `.env` por su cuenta (backend, analítica, latido, central) no las reciben: así un `.env` que cambia con el
//!   servicio en marcha no queda tapado por un valor viejo del entorno.

use crate::cli::{Args, CtlError};
use crate::ctx::Ctx;
use crate::runstatus::{Publisher, RunState};
use std::ffi::OsString;
use std::io::{BufRead, BufReader, Read};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::mpsc::{Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use vms_common::exit_codes;
use vms_common::layout::exe_name;
use vms_common::logfile::RotatingLog;
use vms_common::services::{Kind, ServiceDef, BACKEND, HEARTBEAT, UPDATER};
use vms_common::supervise::Backoff;

pub struct RunSpec {
    pub service: String,
    pub program: PathBuf,
    pub args: Vec<OsString>,
    pub cwd: PathBuf,
    pub env_set: Vec<(String, OsString)>,
    pub env_remove: Vec<&'static str>,
    pub log_path: PathBuf,
    pub grace: Duration,
    pub exit_on_crash: bool,
    pub backoff: Backoff,
    /// No arrancar hasta que exista (el YAML del motor lo escribe el backend o el instalador).
    pub wait_for: Option<PathBuf>,
    pub wait_poll: Duration,
    /// No arrancar mientras esta variable falte en el entorno y en el `.env` (estado «en espera», sano).
    pub idle_unless: Option<(&'static str, PathBuf)>,
    pub idle_poll: Duration,
    /// `logs\status-<Servicio>.json` (None en algunas pruebas).
    pub status_path: Option<PathBuf>,
    pub version: Option<String>,
    /// Rotación copiando y vaciando (`engine.log`).
    pub copy_truncate: bool,
}

/// Variable que hace falta para que el servicio tenga algo que hacer en este puesto.
pub fn required_setting(def: &ServiceDef) -> Option<&'static str> {
    (def.name == HEARTBEAT).then_some("VMS_CENTRAL_URL")
}

/// ¿Está `key` en el entorno o en el `.env`, con valor?
pub fn setting_present(key: &str, env_file: &std::path::Path) -> bool {
    std::env::var(key).is_ok_and(|v| !v.trim().is_empty())
        || crate::envfile::read(env_file).get(key).is_some_and(|v| !v.trim().is_empty())
}

/// ¿Recibe el servicio las variables del `.env` en su entorno? Solo el que no lo lee por su cuenta.
pub fn passes_env_file(def: &ServiceDef) -> bool {
    def.name == UPDATER
}

/// Argumentos tras `-m módulo`: `vms_updater` exige la orden (`run` es el servicio; sin ella argparse sale con 2).
pub fn module_args(def: &ServiceDef) -> &'static [&'static str] {
    if def.name == UPDATER {
        &["run"]
    } else {
        &[]
    }
}

/// Variables `VMS_*` con valor del `.env`, en orden, salvo las que ya trae el entorno del proceso.
pub fn env_file_vars(env_file: &std::path::Path) -> Vec<(String, OsString)> {
    let mut vars: Vec<(String, OsString)> = crate::envfile::read(env_file)
        .into_iter()
        .filter(|(k, v)| k.starts_with("VMS_") && !v.trim().is_empty() && std::env::var_os(k).is_none())
        .map(|(k, v)| (k, OsString::from(v)))
        .collect();
    vars.sort();
    vars
}

/// Variables de Python que nunca deben llegar al runtime embebido desde fuera.
const PYTHON_ENV_REMOVE: &[&str] = &["PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE"];

pub fn build_spec(def: &ServiceDef, ctx: &Ctx, a: &Args) -> Result<RunSpec, CtlError> {
    let data = &ctx.data;
    // Primero las del .env: las fijas de abajo van después y ganan (Command::env: la última manda).
    let mut env_set: Vec<(String, OsString)> =
        if passes_env_file(def) { env_file_vars(&data.env_file()) } else { Vec::new() };
    env_set.extend([
        ("VMS_DATA_DIR".into(), data.root.clone().into_os_string()),
        ("PYTHONDONTWRITEBYTECODE".into(), "1".into()),
        ("PYTHONUNBUFFERED".into(), "1".into()),
        ("PYTHONUTF8".into(), "1".into()),
        ("PYTHONIOENCODING".into(), "utf-8".into()),
        ("PYTHONNOUSERSITE".into(), "1".into()),
    ]);
    if let Some(v) = ctx.own_version() {
        env_set.push(("VMS_ACTIVE_VERSION".into(), v.into()));
    }
    let (program, args, cwd, wait_for) = match def.kind {
        Kind::Engine => {
            let program = match a.value("--mediamtx") {
                Some(p) => PathBuf::from(p),
                None => ctx.component_root()?.join("engine").join(exe_name("mediamtx")),
            };
            let yml = data.mediamtx_yml();
            (program, vec![yml.clone().into_os_string()], data.mediamtx_dir(), Some(yml))
        }
        Kind::Python(module) => {
            env_set.push(("VMS_STOP_ON_STDIN_EOF".into(), "1".into()));
            if def.name == BACKEND {
                env_set.push(("VMS_ENGINE_MODE".into(), "attach".into()));
            }
            let program = ctx.python(a.value("--python"))?;
            let cwd = ctx
                .component_root()
                .map(|r| r.join("app"))
                .ok()
                .filter(|p| p.is_dir())
                .unwrap_or_else(|| std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")));
            let mut args: Vec<OsString> = vec!["-m".into(), module.into()];
            args.extend(module_args(def).iter().map(OsString::from));
            (program, args, cwd, None)
        }
    };
    Ok(RunSpec {
        service: def.name.to_string(),
        program,
        args,
        cwd,
        env_set,
        env_remove: PYTHON_ENV_REMOVE.to_vec(),
        log_path: data.logs_dir().join(def.log_name()),
        grace: Duration::from_secs(def.grace_s),
        exit_on_crash: a.has("--exit-on-crash"),
        backoff: Backoff::standard(),
        wait_for,
        wait_poll: Duration::from_secs(2),
        idle_unless: required_setting(def).map(|k| (k, data.env_file())),
        idle_poll: Duration::from_secs(30),
        status_path: Some(crate::runstatus::path_for(data, def.name)),
        version: ctx.own_version().map(str::to_string),
        copy_truncate: def.kind == Kind::Engine,
    })
}

type SharedLog = Arc<Mutex<RotatingLog>>;

fn event(log: &SharedLog, msg: &str) {
    if let Ok(mut l) = log.lock() {
        l.event(&format!("[vmsctl] {msg}"));
    }
}

fn pump(pipe: impl Read + Send + 'static, log: SharedLog) {
    std::thread::spawn(move || {
        let mut reader = BufReader::new(pipe);
        let mut buf = Vec::new();
        loop {
            buf.clear();
            match reader.read_until(b'\n', &mut buf) {
                Ok(0) | Err(_) => return,
                Ok(_) => {
                    let line = String::from_utf8_lossy(&buf);
                    let line = line.trim_end_matches(['\r', '\n']);
                    if !line.is_empty() {
                        if let Ok(mut l) = log.lock() {
                            let _ = l.write_line(line);
                        }
                    }
                }
            }
        }
    });
}

struct Proc {
    child: std::process::Child,
    stdin: Option<std::process::ChildStdin>,
    #[cfg(windows)]
    job: vms_common::winjob::Job,
}

impl Proc {
    fn spawn(spec: &RunSpec, log: &SharedLog) -> std::io::Result<Proc> {
        let mut cmd = Command::new(&spec.program);
        cmd.args(&spec.args).current_dir(&spec.cwd).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
        for k in &spec.env_remove {
            cmd.env_remove(k);
        }
        for (k, v) in &spec.env_set {
            cmd.env(k, v);
        }
        #[cfg(windows)]
        let (mut child, job) = {
            let job = vms_common::winjob::Job::new()?;
            let child = vms_common::winjob::spawn_in_job(&mut cmd, &job)?;
            (child, job)
        };
        #[cfg(not(windows))]
        let mut child = {
            use std::os::unix::process::CommandExt;
            cmd.process_group(0);
            cmd.spawn()?
        };
        if let Some(out) = child.stdout.take() {
            pump(out, log.clone());
        }
        if let Some(err) = child.stderr.take() {
            pump(err, log.clone());
        }
        let stdin = child.stdin.take();
        Ok(Proc {
            child,
            stdin,
            #[cfg(windows)]
            job,
        })
    }

    /// Primer escalón: «para, por favor».
    fn soft_stop(&mut self) {
        self.stdin = None;
        #[cfg(not(windows))]
        {
            let _ = Command::new("kill")
                .args(["-TERM", &self.child.id().to_string()])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
    }

    /// Último escalón: el proceso y todo lo que haya lanzado.
    fn hard_kill(&mut self) {
        #[cfg(windows)]
        self.job.terminate();
        #[cfg(not(windows))]
        {
            let _ = Command::new("kill")
                .args(["-KILL", "--", &format!("-{}", self.child.id())])
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
            let _ = self.child.kill();
        }
    }

    fn stop(&mut self, grace: Duration, log: &SharedLog) {
        if grace > Duration::ZERO {
            self.soft_stop();
            let t0 = Instant::now();
            while t0.elapsed() < grace {
                if let Ok(Some(_)) = self.child.try_wait() {
                    event(log, "proceso parado de forma ordenada");
                    return;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            event(log, &format!("no paró en {} s: se fuerza", grace.as_secs()));
        }
        self.hard_kill();
        let _ = self.child.wait();
    }
}

fn stop_requested(stop: &Receiver<()>, wait: Duration) -> bool {
    match stop.recv_timeout(wait) {
        Ok(()) | Err(RecvTimeoutError::Disconnected) => true,
        Err(RecvTimeoutError::Timeout) => false,
    }
}

/// Estado publicado (si hay ruta) con aviso en el registro si no se puede escribir.
struct Status(Option<Publisher>);

impl Status {
    fn set(&mut self, log: &SharedLog, state: RunState, detail: &str, pid: Option<u32>) {
        if let Some(msg) = self.0.as_mut().and_then(|p| p.set(state, detail, pid)) {
            event(log, &msg);
        }
    }
    fn crashed(&mut self, log: &SharedLog, detail: &str, state: RunState) {
        if let Some(msg) = self.0.as_mut().and_then(|p| p.crashed(detail, state)) {
            event(log, &msg);
        }
    }
    fn refresh(&mut self, log: &SharedLog) {
        if let Some(msg) = self.0.as_mut().and_then(Publisher::refresh) {
            event(log, &msg);
        }
    }
}

/// Espera (publicando el estado) hasta que se cumpla `ready` o se pida parar. `true` = parar.
fn wait_until(
    stop: &Receiver<()>,
    poll: Duration,
    status: &mut Status,
    log: &SharedLog,
    ready: impl Fn() -> bool,
) -> bool {
    while !ready() {
        if stop_requested(stop, poll) {
            return true;
        }
        status.refresh(log);
    }
    false
}

/// Bucle del anfitrión. Devuelve el código de salida de `vmsctl`.
pub fn run(spec: &mut RunSpec, stop: &Receiver<()>) -> i32 {
    let log: SharedLog = Arc::new(Mutex::new(if spec.copy_truncate {
        RotatingLog::copy_truncate(
            &spec.log_path,
            vms_common::logfile::DEFAULT_MAX_BYTES,
            vms_common::logfile::DEFAULT_BACKUPS,
        )
    } else {
        RotatingLog::standard(&spec.log_path)
    }));
    let mut status =
        Status(spec.status_path.clone().map(|p| Publisher::new(p, &spec.service, spec.version.as_deref())));
    event(&log, &format!("{}: arranco {} (pid {})", spec.service, spec.program.display(), std::process::id()));
    let code = run_loop(spec, stop, &log, &mut status);
    if code == exit_codes::OK {
        status.set(&log, RunState::Stopped, "parado", None);
    }
    code
}

fn run_loop(spec: &mut RunSpec, stop: &Receiver<()>, log: &SharedLog, status: &mut Status) -> i32 {
    loop {
        if let Some((key, env_file)) = spec.idle_unless.clone() {
            if !setting_present(key, &env_file) {
                let why = format!("sin {key}: no hay nada que hacer en este puesto hasta que se configure");
                event(log, &why);
                status.set(log, RunState::Idle, &why, None);
                if wait_until(stop, spec.idle_poll, status, log, || setting_present(key, &env_file)) {
                    return exit_codes::OK;
                }
                event(log, &format!("{key} configurada: arranco"));
            }
        }
        if let Some(w) = spec.wait_for.clone() {
            if !w.is_file() {
                let why = format!("espero a que exista {}", w.display());
                event(log, &why);
                status.set(log, RunState::Waiting, &why, None);
                if wait_until(stop, spec.wait_poll, status, log, || w.is_file()) {
                    return exit_codes::OK;
                }
            }
        }
        if !spec.program.is_file() {
            let why = format!("no existe {}: la instalación está incompleta", spec.program.display());
            event(log, &why);
            status.crashed(log, &why, RunState::Failed);
            return exit_codes::WINDOWS_ERROR;
        }
        let mut proc = match Proc::spawn(spec, log) {
            Ok(p) => p,
            Err(e) => {
                let why = format!("no se pudo lanzar {}: {e}", spec.program.display());
                event(log, &why);
                status.crashed(log, &why, RunState::Failed);
                return exit_codes::WINDOWS_ERROR;
            }
        };
        let started = Instant::now();
        let pid = proc.child.id();
        event(log, &format!("proceso en marcha (pid {pid})"));
        status.set(log, RunState::Running, "", Some(pid));
        let code = loop {
            if stop_requested(stop, Duration::from_millis(200)) {
                proc.stop(spec.grace, log);
                event(log, "parado");
                return exit_codes::OK;
            }
            match proc.child.try_wait() {
                Ok(Some(st)) => break st.code().unwrap_or(-1),
                Ok(None) => status.refresh(log),
                Err(e) => {
                    event(log, &format!("no se pudo consultar el proceso: {e}"));
                    proc.hard_kill();
                    break -1;
                }
            }
        };
        drop(proc); // en Windows cierra el job: si quedaba algún nieto, muere
        let why = format!("el proceso terminó con código {code} tras {} s", started.elapsed().as_secs());
        event(log, &why);
        if spec.exit_on_crash {
            status.crashed(log, &why, RunState::Failed);
            return exit_codes::CHILD_EXITED;
        }
        status.crashed(log, &why, RunState::Backoff);
        let wait = spec.backoff.after_crash(started.elapsed());
        event(log, &format!("lo relanzo dentro de {} s", wait.as_secs_f32()));
        if stop_requested(stop, wait) {
            return exit_codes::OK;
        }
    }
}

/// `--stop-on-stdin-eof`: un hilo lee la entrada estándar y avisa cuando se cierra.
pub fn stdin_eof_signal(tx: std::sync::mpsc::Sender<()>) {
    std::thread::spawn(move || {
        let _ = std::io::copy(&mut std::io::stdin(), &mut std::io::sink());
        let _ = tx.send(());
    });
}

#[cfg(all(test, unix))]
pub fn log_path_for(spec: &RunSpec) -> &std::path::Path {
    &spec.log_path
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::path::Path;
    use std::sync::mpsc;

    fn spec(dir: &Path, script: &str) -> RunSpec {
        RunSpec {
            service: "VMSPrueba".into(),
            program: PathBuf::from("/bin/sh"),
            args: vec!["-c".into(), script.into()],
            cwd: dir.to_path_buf(),
            env_set: vec![("VMS_PRUEBA".into(), "1".into())],
            env_remove: vec!["PYTHONPATH"],
            log_path: dir.join("logs").join("VMSPrueba.log"),
            grace: Duration::from_millis(500),
            exit_on_crash: false,
            backoff: Backoff::new(vec![Duration::from_millis(50)], Duration::from_secs(60)),
            wait_for: None,
            wait_poll: Duration::from_millis(50),
            idle_unless: None,
            idle_poll: Duration::from_millis(50),
            status_path: Some(dir.join("logs").join("status-VMSPrueba.json")),
            version: Some("2.0.0".into()),
            copy_truncate: false,
        }
    }

    fn status_of(s: &RunSpec) -> crate::runstatus::RunStatus {
        crate::runstatus::read(s.status_path.as_ref().unwrap()).expect("estado publicado")
    }

    fn read_log(s: &RunSpec) -> String {
        std::thread::sleep(Duration::from_millis(200)); // los hilos de lectura terminan de escribir
        std::fs::read_to_string(log_path_for(s)).unwrap_or_default()
    }

    #[test]
    fn output_is_logged_without_credentials_and_crash_exits_with_13() {
        let d = tempfile::tempdir().unwrap();
        let mut s = spec(d.path(), "echo \"ERR rtsp://admin:Cl4ve%21@10.0.0.1/x VMS_PRUEBA=$VMS_PRUEBA\"; exit 3");
        s.exit_on_crash = true;
        let (_tx, rx) = mpsc::channel();
        assert_eq!(run(&mut s, &rx), exit_codes::CHILD_EXITED);
        let text = read_log(&s);
        assert!(text.contains("rtsp://***:***@10.0.0.1/x VMS_PRUEBA=1"), "{text}");
        assert!(!text.contains("Cl4ve") && text.contains("código 3"), "{text}");
    }

    #[test]
    fn status_reports_a_crash_loop_and_a_healthy_child() {
        // Regresión: health wait daba «sano» con la analítica en bucle de caídas (el SCM solo ve vmshost).
        let d = tempfile::tempdir().unwrap();
        let mut s = spec(d.path(), "exit 1");
        let path = s.status_path.clone().unwrap();
        let (tx, rx) = mpsc::channel();
        let h = std::thread::spawn(move || run(&mut s, &rx));
        // Espera a ver dos caídas (en un runner cargado, 600 ms fijos no siempre bastaban): como mucho 5 s.
        let t0 = Instant::now();
        let st = loop {
            std::thread::sleep(Duration::from_millis(100));
            if let Some(st) = crate::runstatus::read(&path) {
                if st.crashes_unix.len() >= 2 || t0.elapsed() > Duration::from_secs(5) {
                    break st;
                }
            }
        };
        assert!(st.crashes_unix.len() >= 2, "{st:?}");
        let (ok, why) = crate::runstatus::evaluate(Some(&st), vms_common::state::now_unix());
        assert!(!ok, "{why}");
        tx.send(()).unwrap();
        assert_eq!(h.join().unwrap(), exit_codes::OK);
        assert_eq!(crate::runstatus::read(&path).unwrap().state, crate::runstatus::RunState::Stopped);

        let mut s = spec(d.path(), "sleep 30");
        s.status_path = Some(d.path().join("logs").join("status-Otro.json"));
        let (tx, rx) = mpsc::channel();
        let path2 = s.status_path.clone().unwrap();
        let h = std::thread::spawn(move || run(&mut s, &rx));
        std::thread::sleep(Duration::from_millis(400));
        let st = crate::runstatus::read(&path2).unwrap();
        assert_eq!(st.state, crate::runstatus::RunState::Running);
        assert!(st.child_pid.is_some() && st.started_unix.is_some() && st.crashes_unix.is_empty());
        tx.send(()).unwrap();
        assert_eq!(h.join().unwrap(), exit_codes::OK);
    }

    #[test]
    fn heartbeat_without_central_url_idles_and_starts_once_configured() {
        let d = tempfile::tempdir().unwrap();
        let env_file = d.path().join(".env");
        let marks = d.path().join("ran");
        let mut s = spec(d.path(), &format!("echo x > {}; sleep 30", marks.display()));
        s.idle_unless = Some(("VMS_PRUEBA_CENTRAL_URL", env_file.clone()));
        let (tx, rx) = mpsc::channel();
        let h = std::thread::spawn(move || run(&mut s, &rx));
        std::thread::sleep(Duration::from_millis(300));
        assert!(!marks.exists(), "sin la variable no se lanza");
        let st = crate::runstatus::read(&d.path().join("logs").join("status-VMSPrueba.json")).unwrap();
        assert_eq!(st.state, crate::runstatus::RunState::Idle);
        assert!(crate::runstatus::evaluate(Some(&st), vms_common::state::now_unix()).0, "en espera es sano");
        std::fs::write(&env_file, "VMS_PRUEBA_CENTRAL_URL=https://central.example\n").unwrap();
        std::thread::sleep(Duration::from_millis(400));
        assert!(marks.exists(), "arranca en cuanto se configura");
        tx.send(()).unwrap();
        assert_eq!(h.join().unwrap(), exit_codes::OK);
        assert!(super::required_setting(vms_common::services::by_name("VMSHeartbeat").unwrap()).is_some());
        assert!(super::required_setting(vms_common::services::by_name("VMSBackend").unwrap()).is_none());
    }

    #[test]
    fn crashing_child_is_relaunched_until_stopped() {
        let d = tempfile::tempdir().unwrap();
        let marks = d.path().join("marks");
        let mut s = spec(d.path(), &format!("echo x >> {}; exit 1", marks.display()));
        let (tx, rx) = mpsc::channel();
        let h = std::thread::spawn(move || run(&mut s, &rx));
        std::thread::sleep(Duration::from_millis(1500));
        tx.send(()).unwrap();
        assert_eq!(h.join().unwrap(), exit_codes::OK);
        let n = std::fs::read_to_string(&marks).unwrap().lines().count();
        assert!(n >= 3, "relanzado {n} veces");
    }

    #[test]
    fn stop_closes_stdin_first() {
        let d = tempfile::tempdir().unwrap();
        let out = d.path().join("eof");
        let mut s = spec(d.path(), &format!("trap '' TERM; cat >/dev/null; echo ok > {}", out.display()));
        s.grace = Duration::from_secs(5);
        let (tx, rx) = mpsc::channel();
        let h = std::thread::spawn(move || run(&mut s, &rx));
        std::thread::sleep(Duration::from_millis(400));
        let t0 = Instant::now();
        tx.send(()).unwrap();
        assert_eq!(h.join().unwrap(), exit_codes::OK);
        assert!(t0.elapsed() < Duration::from_secs(3), "paró sin esperar al último escalón");
        assert_eq!(std::fs::read_to_string(&out).unwrap().trim(), "ok");
    }

    #[test]
    fn stubborn_child_and_its_children_are_killed_after_the_grace() {
        let d = tempfile::tempdir().unwrap();
        let pidfile = d.path().join("pid");
        let mut s = spec(d.path(), &format!("trap '' TERM; sleep 30 & echo $! > {}; wait", pidfile.display()));
        s.grace = Duration::from_millis(300);
        let (tx, rx) = mpsc::channel();
        let h = std::thread::spawn(move || run(&mut s, &rx));
        std::thread::sleep(Duration::from_millis(400));
        let t0 = Instant::now();
        tx.send(()).unwrap();
        assert_eq!(h.join().unwrap(), exit_codes::OK);
        assert!(t0.elapsed() < Duration::from_secs(3));
        let pid = std::fs::read_to_string(&pidfile).unwrap().trim().to_string();
        std::thread::sleep(Duration::from_millis(200));
        let alive = Command::new("kill").args(["-0", &pid]).status().unwrap().success();
        assert!(!alive, "el nieto {pid} sigue vivo");
    }

    #[test]
    fn engine_waits_for_its_yaml() {
        let d = tempfile::tempdir().unwrap();
        let yml = d.path().join("mediamtx.yml");
        let marks = d.path().join("ran");
        let mut s = spec(d.path(), &format!("echo x > {}; sleep 5", marks.display()));
        s.wait_for = Some(yml.clone());
        let (tx, rx) = mpsc::channel();
        let h = std::thread::spawn(move || run(&mut s, &rx));
        std::thread::sleep(Duration::from_millis(300));
        assert!(!marks.exists(), "no arranca sin el YAML");
        std::fs::write(&yml, "paths: {}\n").unwrap();
        std::thread::sleep(Duration::from_millis(400));
        assert!(marks.exists(), "arranca en cuanto aparece");
        tx.send(()).unwrap();
        assert_eq!(h.join().unwrap(), exit_codes::OK);
    }

    #[test]
    fn the_updater_gets_the_env_file_and_the_backend_reads_it_itself() {
        // Hallazgo A3: el instalador escribe VMS_UPDATE_SOURCE (y los puertos) en el .env, y vms_updater solo
        // mira su entorno. Antes no le llegaba nada: el actualizador corría sin fuente de actualizaciones.
        use vms_common::layout::Component;
        let d = tempfile::tempdir().unwrap();
        let slot = d.path().join("pf").join("updater").join("slot-a");
        std::fs::create_dir_all(slot.join("app")).unwrap();
        let comp = Component::UpdaterSlot { slot: "a".into(), root: slot.clone() };
        let ctx = Ctx::for_tests(&d.path().join("pd"), Some(&d.path().join("pf")), Some(comp));
        std::fs::create_dir_all(&ctx.data.root).unwrap();
        std::fs::write(
            ctx.data.env_file(),
            "VMS_UPDATE_SOURCE=http://127.0.0.1:8765/\nVMS_HTTP_PORT=8601\nVMS_DATA_DIR=C:\\otra\nVMS_VACIA=\nOTRA=1\n",
        )
        .unwrap();
        let a = Args::parse(&["run".into(), "--service".into(), "VMSUpdater".into()]).unwrap();
        let s = build_spec(vms_common::services::by_name("VMSUpdater").unwrap(), &ctx, &a).unwrap();
        let last = |k: &str| s.env_set.iter().rev().find(|(n, _)| n == k).map(|(_, v)| v.clone());
        assert_eq!(last("VMS_UPDATE_SOURCE"), Some("http://127.0.0.1:8765/".into()));
        assert_eq!(last("VMS_HTTP_PORT"), Some("8601".into()));
        assert_eq!(last("VMS_DATA_DIR"), Some(ctx.data.root.clone().into_os_string()), "la carpeta real manda");
        assert_eq!(last("VMS_VACIA"), None);
        assert_eq!(last("OTRA"), None, "solo variables VMS_*");
        assert_eq!(s.cwd, slot.join("app"));
        // CI B3: sin «run» el actualizador salía con 2 al instante, en bucle
        assert_eq!(s.args, vec![OsString::from("-m"), "vms_updater".into(), "run".into()]);
        let b = build_spec(vms_common::services::by_name("VMSBackend").unwrap(), &ctx, &a).unwrap();
        assert!(!b.env_set.iter().any(|(n, _)| n == "VMS_UPDATE_SOURCE"), "el backend lee el .env él mismo");
        assert_eq!(b.args, vec![OsString::from("-m"), "vms".into()]);
    }

    #[test]
    fn missing_program_is_an_error_for_vmshost_to_retry() {
        let d = tempfile::tempdir().unwrap();
        let mut s = spec(d.path(), "");
        s.program = d.path().join("no-existe");
        let (_tx, rx) = mpsc::channel();
        assert_eq!(run(&mut s, &rx), exit_codes::WINDOWS_ERROR);
        assert!(read_log(&s).contains("instalación está incompleta"));
        assert_eq!(status_of(&s).state, crate::runstatus::RunState::Failed);
    }
}
