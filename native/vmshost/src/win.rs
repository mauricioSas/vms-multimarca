//! Parte de Windows del arrancador: servicio del SCM (windows-service-rs) y lanzador con Job Object.
//!
//! El hijo se crea **suspendido**, se mete en el Job Object «kill on close» y solo entonces se reanuda:
//! no hay ningún instante en el que pueda crear procesos fuera del job (CONTRATO §13.3, hallazgo de S4).

use crate::host::{Child, Host, HostConfig, Launcher};
use std::ffi::{c_void, OsString};
use std::io;
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::path::Path;
use std::process::{ChildStdin, Command, ExitCode, Stdio};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::sync::OnceLock;
use std::time::{Duration, Instant};
use vms_common::state::now_unix;
use windows_service::service::{
    ServiceControl, ServiceControlAccept, ServiceExitCode, ServiceState, ServiceStatus, ServiceType,
};
use windows_service::service_control_handler::{self, ServiceControlHandlerResult};
use windows_service::{define_windows_service, service_dispatcher};
use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD, THREADENTRY32,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation, SetInformationJobObject,
    TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
use windows_sys::Win32::System::Threading::{
    OpenThread, ResumeThread, CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, CREATE_SUSPENDED, THREAD_SUSPEND_RESUME,
};

/// Job Object «kill on close»: si el arrancador muere, Windows mata a todos los hijos.
pub struct Job(HANDLE);

// SAFETY: un HANDLE de job se puede usar desde cualquier hilo.
unsafe impl Send for Job {}
unsafe impl Sync for Job {}

impl Job {
    pub fn new() -> io::Result<Job> {
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

    pub fn assign(&self, process: HANDLE) -> io::Result<()> {
        // SAFETY: handles válidos mientras vivan el job y el proceso.
        if unsafe { AssignProcessToJobObject(self.0, process) } == 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    }

    pub fn terminate(&self) {
        // SAFETY: handle propio y válido.
        unsafe { TerminateJobObject(self.0, 1) };
    }
}

impl Drop for Job {
    fn drop(&mut self) {
        // SAFETY: se cierra nuestro handle una sola vez.
        unsafe { CloseHandle(self.0) };
    }
}

/// Reanuda el único hilo de un proceso recién creado con `CREATE_SUSPENDED`.
pub fn resume_process(pid: u32) -> io::Result<()> {
    // SAFETY: instantánea de hilos del sistema; se recorre con una estructura propia y se cierra.
    unsafe {
        let snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
        if snap == INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error());
        }
        let mut entry: THREADENTRY32 = std::mem::zeroed();
        entry.dwSize = std::mem::size_of::<THREADENTRY32>() as u32;
        let mut resumed = 0;
        let mut ok = Thread32First(snap, &mut entry);
        while ok != 0 {
            if entry.th32OwnerProcessID == pid {
                let t = OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID);
                if !t.is_null() {
                    if ResumeThread(t) != u32::MAX {
                        resumed += 1;
                    }
                    CloseHandle(t);
                }
            }
            ok = Thread32Next(snap, &mut entry);
        }
        CloseHandle(snap);
        if resumed == 0 {
            return Err(io::Error::other(format!("no se encontró el hilo del proceso {pid} para reanudarlo")));
        }
    }
    Ok(())
}

/// Lanza `exe` suspendido, lo mete en `job` y lo reanuda. Si algo falla, el hijo se mata: nunca queda
/// un proceso fuera del job.
pub fn spawn_in_job(cmd: &mut Command, job: &Job) -> io::Result<std::process::Child> {
    cmd.creation_flags(CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP);
    let mut child = cmd.spawn()?;
    let setup = job.assign(child.as_raw_handle() as HANDLE).and_then(|()| resume_process(child.id()));
    if let Err(e) = setup {
        let _ = child.kill();
        let _ = child.wait();
        return Err(e);
    }
    Ok(child)
}

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
