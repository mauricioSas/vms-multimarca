//! `vmsctl run --service <S>`: anfitrión del proceso real de cada servicio (CONTRATO §14.1).
//!
//! - Lanza `runtime\python.exe -m <módulo>` o `engine\mediamtx.exe <datos>\mediamtx\mediamtx.yml` dentro de
//!   su propio Job Object (creado suspendido): si `vmsctl` muere, muere el proceso.
//! - Copia su salida, línea a línea y **sin credenciales**, a `logs\<servicio>.log` (MediaMTX en
//!   `logs\engine.log`, que el backend sigue con `vms/engine/logtail.py`), con rotación 10 × 10 MB.
//! - Parada en 3 escalones: cierra la entrada estándar del proceso (el backend la vigila con
//!   `VMS_STOP_ON_STDIN_EOF=1`; en POSIX además SIGTERM) → espera `grace` (10 s; 0 para MediaMTX, que no
//!   necesita parada ordenada) → `TerminateJobObject`.
//! - Si el proceso cae, lo relanza con espera creciente (1, 2, 5, 10, 30 s). Con `--exit-on-crash`
//!   (versión a prueba) sale con el código 13 para que `vmshost` cuente la caída.
//! - Para cuando `vmshost` cierra su entrada estándar (`--stop-on-stdin-eof`).

use crate::cli::{Args, CtlError};
use crate::ctx::Ctx;
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
use vms_common::services::{Kind, ServiceDef, BACKEND};
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
}

/// Variables de Python que nunca deben llegar al runtime embebido desde fuera.
const PYTHON_ENV_REMOVE: &[&str] = &["PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE"];

pub fn build_spec(def: &ServiceDef, ctx: &Ctx, a: &Args) -> Result<RunSpec, CtlError> {
    let data = &ctx.data;
    let mut env_set: Vec<(String, OsString)> = vec![
        ("VMS_DATA_DIR".into(), data.root.clone().into_os_string()),
        ("PYTHONDONTWRITEBYTECODE".into(), "1".into()),
        ("PYTHONUNBUFFERED".into(), "1".into()),
        ("PYTHONUTF8".into(), "1".into()),
        ("PYTHONIOENCODING".into(), "utf-8".into()),
        ("PYTHONNOUSERSITE".into(), "1".into()),
    ];
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
            (program, vec!["-m".into(), module.into()], cwd, None)
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

/// Bucle del anfitrión. Devuelve el código de salida de `vmsctl`.
pub fn run(spec: &mut RunSpec, stop: &Receiver<()>) -> i32 {
    let log: SharedLog = Arc::new(Mutex::new(RotatingLog::standard(&spec.log_path)));
    event(&log, &format!("{}: arranco {} (pid {})", spec.service, spec.program.display(), std::process::id()));
    loop {
        if let Some(w) = spec.wait_for.clone() {
            let mut said = false;
            while !w.is_file() {
                if !said {
                    event(&log, &format!("espero a que exista {}", w.display()));
                    said = true;
                }
                if stop_requested(stop, spec.wait_poll) {
                    return exit_codes::OK;
                }
            }
        }
        if !spec.program.is_file() {
            event(&log, &format!("no existe {}: la instalación está incompleta", spec.program.display()));
            return exit_codes::WINDOWS_ERROR;
        }
        let mut proc = match Proc::spawn(spec, &log) {
            Ok(p) => p,
            Err(e) => {
                event(&log, &format!("no se pudo lanzar {}: {e}", spec.program.display()));
                return exit_codes::WINDOWS_ERROR;
            }
        };
        let started = Instant::now();
        event(&log, &format!("proceso en marcha (pid {})", proc.child.id()));
        let code = loop {
            if stop_requested(stop, Duration::from_millis(200)) {
                proc.stop(spec.grace, &log);
                event(&log, "parado");
                return exit_codes::OK;
            }
            match proc.child.try_wait() {
                Ok(Some(status)) => break status.code().unwrap_or(-1),
                Ok(None) => {}
                Err(e) => {
                    event(&log, &format!("no se pudo consultar el proceso: {e}"));
                    proc.hard_kill();
                    break -1;
                }
            }
        };
        drop(proc); // en Windows cierra el job: si quedaba algún nieto, muere
        event(&log, &format!("el proceso terminó con código {code} tras {} s", started.elapsed().as_secs()));
        if spec.exit_on_crash {
            return exit_codes::CHILD_EXITED;
        }
        let wait = spec.backoff.after_crash(started.elapsed());
        event(&log, &format!("lo relanzo dentro de {} s", wait.as_secs_f32()));
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
        }
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
    fn missing_program_is_an_error_for_vmshost_to_retry() {
        let d = tempfile::tempdir().unwrap();
        let mut s = spec(d.path(), "");
        s.program = d.path().join("no-existe");
        let (_tx, rx) = mpsc::channel();
        assert_eq!(run(&mut s, &rx), exit_codes::WINDOWS_ERROR);
        assert!(read_log(&s).contains("instalación está incompleta"));
    }
}
