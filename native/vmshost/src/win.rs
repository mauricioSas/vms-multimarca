//! Parte de Windows del arrancador: servicio del SCM (windows-service-rs) y lanzador con Job Object.
//!
//! El hijo se crea **suspendido**, se mete en el Job Object «kill on close» y solo entonces se reanuda:
//! no hay ningún instante en el que pueda crear procesos fuera del job (CONTRATO §13.3, hallazgo de S4).

use crate::host::{Child, Host, HostConfig, Launcher};
use std::ffi::OsString;
use std::io;
use std::path::Path;
use std::process::{ChildStdin, Command, ExitCode, Stdio};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::OnceLock;
use std::time::{Duration, Instant};
use vms_common::state::now_unix;
use vms_common::winjob::{spawn_in_job, Job};
use windows_service::service::{
    ServiceControl, ServiceControlAccept, ServiceExitCode, ServiceState, ServiceStatus, ServiceType,
};
use windows_service::service_control_handler::{self, ServiceControlHandlerResult};
use windows_service::{define_windows_service, service_dispatcher};

pub struct JobLauncher {
    job: std::sync::Arc<Job>,
}

impl JobLauncher {
    pub fn new() -> io::Result<Self> {
        Ok(Self { job: std::sync::Arc::new(Job::new()?) })
    }
}

struct WinChild {
    child: std::process::Child,
    stdin: Option<ChildStdin>,
    job: std::sync::Arc<Job>,
}

impl Child for WinChild {
    fn id(&self) -> u32 {
        self.child.id()
    }
    fn try_wait(&mut self) -> io::Result<Option<i32>> {
        Ok(self.child.try_wait()?.map(|s| s.code().unwrap_or(1)))
    }
    fn request_stop(&mut self) {
        self.stdin = None; // EOF en la entrada de `vmsctl run --stop-on-stdin-eof`
    }
    fn kill(&mut self) {
        self.job.terminate();
    }
}

impl Launcher for JobLauncher {
    fn launch(&mut self, exe: &Path, args: &[String]) -> io::Result<Box<dyn Child>> {
        let mut cmd = Command::new(exe);
        cmd.args(args)
            .current_dir(exe.parent().unwrap_or_else(|| Path::new(".")))
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        let mut child = spawn_in_job(&mut cmd, &self.job)?;
        let stdin = child.stdin.take();
        Ok(Box::new(WinChild { child, stdin, job: self.job.clone() }))
    }
}

static CONFIG: OnceLock<HostConfig> = OnceLock::new();

define_windows_service!(ffi_service_main, service_main);

/// `vmshost service --name X`: lo lanza el SCM.
pub fn run_service(cfg: HostConfig) -> ExitCode {
    let name = cfg.service.clone();
    let _ = CONFIG.set(cfg);
    match service_dispatcher::start(&name, ffi_service_main) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!(
                "vmshost: no se pudo conectar con el administrador de servicios ({e}). \
                 Este modo lo usa Windows; para probar en una consola usa «--foreground»."
            );
            ExitCode::from(20)
        }
    }
}

fn service_main(_arguments: Vec<OsString>) {
    let Some(cfg) = CONFIG.get().cloned() else { return };
    let (tx, rx) = mpsc::channel::<()>();
    let handler = move |control| match control {
        ServiceControl::Stop | ServiceControl::Shutdown | ServiceControl::Preshutdown => {
            let _ = tx.send(());
            ServiceControlHandlerResult::NoError
        }
        ServiceControl::Interrogate => ServiceControlHandlerResult::NoError,
        _ => ServiceControlHandlerResult::NotImplemented,
    };
    let Ok(status) = service_control_handler::register(&cfg.service, handler) else { return };
    let grace = cfg.stop_grace;
    let set = |state, accept, code: u32, wait: Duration| {
        let _ = status.set_service_status(ServiceStatus {
            service_type: ServiceType::OWN_PROCESS,
            current_state: state,
            controls_accepted: accept,
            exit_code: ServiceExitCode::Win32(code),
            checkpoint: 0,
            wait_hint: wait,
            process_id: None,
        });
    };
    let launcher = match JobLauncher::new() {
        Ok(l) => l,
        Err(e) => {
            let mut host = Host::new(cfg, NoLauncher);
            host.log(&format!("no se pudo crear el Job Object: {e}"));
            set(ServiceState::Stopped, ServiceControlAccept::empty(), 1, Duration::ZERO);
            return;
        }
    };
    let mut host = Host::new(cfg, launcher);
    set(ServiceState::Running, ServiceControlAccept::STOP | ServiceControlAccept::SHUTDOWN, 0, Duration::ZERO);
    host.log(&format!("en marcha (pid {}, vmshost {})", std::process::id(), env!("CARGO_PKG_VERSION")));
    loop {
        host.tick(Instant::now(), now_unix());
        match rx.recv_timeout(Duration::from_millis(500)) {
            Err(RecvTimeoutError::Timeout) => continue,
            Ok(()) | Err(RecvTimeoutError::Disconnected) => break,
        }
    }
    set(ServiceState::StopPending, ServiceControlAccept::empty(), 0, grace + Duration::from_secs(5));
    host.log("parada pedida por Windows");
    let t0 = Instant::now();
    host.begin_shutdown(t0);
    while !host.poll_shutdown(Instant::now()) {
        if t0.elapsed() > grace + Duration::from_secs(5) {
            break; // el job se cierra al salir y mata lo que quede
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    host.log("parado");
    set(ServiceState::Stopped, ServiceControlAccept::empty(), 0, Duration::ZERO);
}

/// Lanzador vacío para registrar un error antes de salir.
struct NoLauncher;

impl Launcher for NoLauncher {
    fn launch(&mut self, _exe: &Path, _args: &[String]) -> io::Result<Box<dyn Child>> {
        Err(io::Error::other("sin Job Object no se lanza nada"))
    }
}
