//! `vmshost-s4`: arrancador mínimo de la prueba S4.
//!
//!     vmshost-s4 service --name VMSS4Hello --root C:\vms-s4 [--confirm-timeout 1800]   (lo lanza el SCM)
//!     vmshost-s4 switch  --root C:\vms-s4 --to 2.0.0     (vmsctl version switch: deja la versión «a prueba»)
//!     vmshost-s4 confirm --root C:\vms-s4                (el actualizador confirma la versión)
//!     vmshost-s4 show    --root C:\vms-s4
//!
//! Como servicio: lee `state\active.json` (lo reconstruye desde el diario si falta o está corrupto),
//! lanza `versions\<activa>\hola.exe` dentro de un Job Object «kill on close» y vigila la versión a
//! prueba: 3 caídas en 10 min o sin confirmar en `--confirm-timeout` → vuelve a la anterior.

use s4_servicio_windows::Layout;
use std::process::ExitCode;

fn arg(args: &[String], name: &str) -> Option<String> {
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1).cloned())
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(cmd) = args.first().cloned() else {
        eprintln!("uso: vmshost-s4 service|switch|confirm|show --root <carpeta> ...");
        return ExitCode::from(2);
    };
    let Some(root) = arg(&args, "--root") else {
        eprintln!("falta --root");
        return ExitCode::from(2);
    };
    let layout = Layout::new(root);
    let result = match cmd.as_str() {
        "switch" => match arg(&args, "--to") {
            Some(v) => layout.switch(&v).map(|p| println!("{}", serde_json::to_string(&p).unwrap_or_default())),
            None => {
                eprintln!("falta --to");
                return ExitCode::from(2);
            }
        },
        "confirm" => layout.confirm().map(|p| println!("{}", serde_json::to_string(&p).unwrap_or_default())),
        "show" => layout.load_or_rebuild().map(|(p, _)| println!("{}", serde_json::to_string(&p).unwrap_or_default())),
        "service" => return service::run(&args),
        other => {
            eprintln!("orden desconocida: {other}");
            return ExitCode::from(2);
        }
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::from(20)
        }
    }
}

#[cfg(not(windows))]
mod service {
    use std::process::ExitCode;
    pub fn run(_args: &[String]) -> ExitCode {
        eprintln!("el modo servicio solo existe en Windows");
        ExitCode::from(2)
    }
}

#[cfg(windows)]
mod service {
    use super::arg;
    use s4_servicio_windows::{append_log, now_s, trial_expired, Backoff, CrashWindow, Layout, Pointer};
    use std::ffi::{c_void, OsString};
    use std::io;
    use std::os::windows::io::AsRawHandle;
    use std::process::{Child, Command, ExitCode, Stdio};
    use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
    use std::sync::OnceLock;
    use std::time::{Duration, Instant};
    use windows_service::service::{
        ServiceControl, ServiceControlAccept, ServiceExitCode, ServiceState, ServiceStatus, ServiceType,
    };
    use windows_service::service_control_handler::{self, ServiceControlHandlerResult};
    use windows_service::{define_windows_service, service_dispatcher};
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, IsProcessInJob, JobObjectExtendedLimitInformation,
        SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    struct Config {
        name: String,
        root: String,
        confirm_timeout: Duration,
    }
    static CONFIG: OnceLock<Config> = OnceLock::new();

    define_windows_service!(ffi_service_main, service_main);

    pub fn run(args: &[String]) -> ExitCode {
        let cfg = Config {
            name: arg(args, "--name").unwrap_or_else(|| "VMSS4Hello".into()),
            root: arg(args, "--root").unwrap_or_default(),
            confirm_timeout: Duration::from_secs(
                arg(args, "--confirm-timeout").and_then(|s| s.parse().ok()).unwrap_or(1800),
            ),
        };
        let name = cfg.name.clone();
        let _ = CONFIG.set(cfg);
        // Los argumentos de binPath llegan por la línea de órdenes; los de StartService, a service_main.
        match service_dispatcher::start(name, ffi_service_main) {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                eprintln!("no se pudo conectar con el SCM (¿lanzado fuera de un servicio?): {e}");
                ExitCode::from(20)
            }
        }
    }

    fn service_main(_arguments: Vec<OsString>) {
        let Some(cfg) = CONFIG.get() else { return };
        let layout = Layout::new(&cfg.root);
        if let Err(e) = run_service(cfg, &layout) {
            append_log(&layout.log(), &format!("error del servicio: {e}"));
        }
    }

    fn run_service(cfg: &Config, layout: &Layout) -> windows_service::Result<()> {
        let (tx, rx) = mpsc::channel::<()>();
        let handler = move |control| match control {
            ServiceControl::Stop | ServiceControl::Shutdown => {
                let _ = tx.send(());
                ServiceControlHandlerResult::NoError
            }
            ServiceControl::Interrogate => ServiceControlHandlerResult::NoError,
            _ => ServiceControlHandlerResult::NotImplemented,
        };
        let status = service_control_handler::register(&cfg.name, handler)?;
        let set = |state, accept, code: u32| {
            status.set_service_status(ServiceStatus {
                service_type: ServiceType::OWN_PROCESS,
                current_state: state,
                controls_accepted: accept,
                exit_code: ServiceExitCode::Win32(code),
                checkpoint: 0,
                wait_hint: Duration::from_secs(10),
                process_id: None,
            })
        };
        set(ServiceState::Running, ServiceControlAccept::STOP | ServiceControlAccept::SHUTDOWN, 0)?;
        append_log(&layout.log(), &format!("servicio {} en marcha (pid {})", cfg.name, std::process::id()));
        let code = match supervise(cfg, layout, &rx) {
            Ok(()) => 0,
            Err(e) => {
                append_log(&layout.log(), &format!("el supervisor terminó con error: {e}"));
                1
            }
        };
        set(ServiceState::Stopped, ServiceControlAccept::empty(), code)?;
        Ok(())
    }

    /// Job Object «kill on close»: si el arrancador muere, Windows mata a todos los hijos.
    struct Job(HANDLE);

    impl Job {
        fn new() -> io::Result<Job> {
            // SAFETY: llamadas Win32 con punteros válidos; la estructura vive durante la llamada.
            unsafe {
                let h = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if h.is_null() {
                    return Err(io::Error::last_os_error());
                }
                let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
                info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                let ok = SetInformationJobObject(
                    h,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const c_void,
                    std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                );
                if ok == 0 {
                    let e = io::Error::last_os_error();
                    CloseHandle(h);
                    return Err(e);
                }
                Ok(Job(h))
            }
        }
        fn assign(&self, child: &Child) -> io::Result<()> {
            // SAFETY: el handle del hijo es válido mientras viva `child`.
            let ok = unsafe { AssignProcessToJobObject(self.0, child.as_raw_handle() as HANDLE) };
            if ok == 0 {
                Err(io::Error::last_os_error())
            } else {
                Ok(())
            }
        }
        fn contains(&self, child: &Child) -> bool {
            let mut result = 0;
            // SAFETY: handles válidos y puntero a un entero local.
            let ok = unsafe { IsProcessInJob(child.as_raw_handle() as HANDLE, self.0, &mut result) };
            ok != 0 && result != 0
        }
        fn terminate(&self) {
            // SAFETY: handle propio y válido.
            unsafe { TerminateJobObject(self.0, 1) };
        }
    }

    impl Drop for Job {
        fn drop(&mut self) {
            // SAFETY: cerramos nuestro propio handle una sola vez.
            unsafe { CloseHandle(self.0) };
        }
    }

    struct Stats {
        rollbacks: u32,
        launches: u32,
        last_event: String,
    }

    fn write_status(layout: &Layout, p: &Pointer, child: Option<(&Child, bool)>, crashes: usize, st: &Stats) {
        let body = serde_json::json!({
            "host_pid": std::process::id(),
            "child_pid": child.map(|(c, _)| c.id()),
            "child_in_job": child.map(|(_, j)| j),
            "active": p.active, "trial": p.trial, "previous": p.previous,
            "crashes_in_window": crashes, "rollbacks": st.rollbacks, "launches": st.launches,
            "last_event": st.last_event, "ts": now_s(),
        });
        let _ = vms_common::atomic_write(&layout.status(), body.to_string().as_bytes());
    }

    fn stop_requested(rx: &Receiver<()>, wait: Duration) -> bool {
        match rx.recv_timeout(wait) {
            Ok(()) | Err(RecvTimeoutError::Disconnected) => true,
            Err(RecvTimeoutError::Timeout) => false,
        }
    }

    fn do_rollback(layout: &Layout, st: &mut Stats, why: &str) -> io::Result<Pointer> {
        let p = layout.rollback()?;
        st.rollbacks += 1;
        st.last_event = format!("vuelta atrás a {} ({why})", p.active);
        append_log(&layout.log(), &st.last_event);
        Ok(p)
    }

    fn supervise(cfg: &Config, layout: &Layout, rx: &Receiver<()>) -> io::Result<()> {
        let job = Job::new()?;
        let mut crashes = CrashWindow::new(3, Duration::from_secs(600));
        let mut backoff = Backoff::new(Duration::from_secs(60));
        let mut st = Stats { rollbacks: 0, launches: 0, last_event: String::new() };
        loop {
            let (mut p, rebuilt) = layout.load_or_rebuild()?;
            if rebuilt {
                st.last_event = format!("puntero reconstruido desde el diario: {}", p.active);
                append_log(&layout.log(), &st.last_event);
            }
            if trial_expired(&p, now_s(), cfg.confirm_timeout) {
                p = do_rollback(layout, &mut st, "sin confirmar a tiempo")?;
                crashes.reset();
            }
            let exe = layout.version_dir(&p.active).join("hola.exe");
            if !exe.is_file() {
                append_log(&layout.log(), &format!("falta {}", exe.display()));
                if p.trial {
                    do_rollback(layout, &mut st, "ejecutable ausente")?;
                    crashes.reset();
                    continue;
                }
                if stop_requested(rx, Duration::from_secs(5)) {
                    return Ok(());
                }
                continue;
            }
            let log = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(layout.root.join("logs").join("hola.log"))?;
            let mut child = Command::new(&exe)
                .args(["--version", &p.active, "--root", &layout.root.to_string_lossy()])
                .stdin(Stdio::null())
                .stdout(log.try_clone()?)
                .stderr(log)
                .spawn()?;
            // Nota para B1: entre spawn y AssignProcessToJobObject hay una ventana mínima en la que el
            // hijo podría crear nietos fuera del job; el producto debe usar CREATE_SUSPENDED o
            // PROC_THREAD_ATTRIBUTE_JOB_LIST. `hola` no crea procesos, así que aquí no importa.
            if let Err(e) = job.assign(&child) {
                append_log(&layout.log(), &format!("no se pudo meter al hijo en el job: {e}"));
            }
            st.launches += 1;
            let in_job = job.contains(&child);
            st.last_event = format!("lanzado {} (pid {}, en job: {in_job})", p.active, child.id());
            append_log(&layout.log(), &st.last_event);
            write_status(layout, &p, Some((&child, in_job)), crashes.count(), &st);
            let started = Instant::now();
            let failed = loop {
                if stop_requested(rx, Duration::from_millis(500)) {
                    job.terminate();
                    let _ = child.wait();
                    append_log(&layout.log(), "parada pedida por el SCM: hijos terminados");
                    return Ok(());
                }
                if let Some(status) = child.try_wait()? {
                    append_log(&layout.log(), &format!("{} terminó: {status}", p.active));
                    break !status.success();
                }
                if started.elapsed() >= Duration::from_secs(1) {
                    let cur = layout.load_or_rebuild().map(|(c, _)| c).unwrap_or_else(|_| p.clone());
                    if cur.active != p.active {
                        append_log(&layout.log(), &format!("el puntero cambió a {}: relanzo", cur.active));
                        let _ = child.kill();
                        let _ = child.wait();
                        crashes.reset();
                        break false;
                    }
                    if trial_expired(&cur, now_s(), cfg.confirm_timeout) {
                        let _ = child.kill();
                        let _ = child.wait();
                        break false; // la vuelta atrás la hace el principio del bucle
                    }
                    if cur != p {
                        // p. ej. la versión se confirmó: host-status.json no se queda con «trial: true»
                        p = cur;
                        write_status(layout, &p, Some((&child, in_job)), crashes.count(), &st);
                    }
                }
            };
            let mut wait = Duration::from_secs(1);
            if failed && p.trial && crashes.record(Instant::now()) {
                do_rollback(layout, &mut st, "3 caídas en 10 min")?;
                crashes.reset();
                backoff.reset();
            } else if failed {
                // Espera creciente: una versión buena que cae en bucle no se relanza cada segundo
                wait = backoff.after_crash(started.elapsed());
                append_log(&layout.log(), &format!("relanzo {} dentro de {} s", p.active, wait.as_secs()));
            } else {
                backoff.reset();
            }
            write_status(layout, &p, None, crashes.count(), &st);
            if stop_requested(rx, wait) {
                return Ok(());
            }
        }
    }
}
