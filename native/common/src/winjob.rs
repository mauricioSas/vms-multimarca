//! Job Object «kill on close» y lanzamiento de procesos suspendidos (solo Windows). Lo usan `vmshost`
//! (para `vmsctl run`) y `vmsctl run` (para el proceso real del servicio).
//!
//! El hijo se crea **suspendido**, se mete en el job y solo entonces se reanuda: no hay ningún instante en
//! el que pueda crear procesos fuera del job (CONTRATO §13.3, hallazgo de S4). Rust estable no permite
//! aún `PROC_THREAD_ATTRIBUTE_JOB_LIST` (`raw_attribute` es inestable en 1.99), así que el hilo se
//! reanuda buscándolo con una instantánea de Toolhelp.

use std::ffi::c_void;
use std::io;
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::process::Command;
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

/// Job Object «kill on close»: si el dueño del handle muere, Windows mata a todos los procesos del job.
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

    pub fn assign(&self, child: &std::process::Child) -> io::Result<()> {
        // SAFETY: handles válidos mientras vivan el job y el proceso (`child` lo mantiene abierto).
        if unsafe { AssignProcessToJobObject(self.0, child.as_raw_handle() as HANDLE) } == 0 {
            Err(io::Error::last_os_error())
        } else {
            Ok(())
        }
    }

    /// Mata todos los procesos del job (tercer escalón de la parada).
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

/// Reanuda los hilos de un proceso recién creado con `CREATE_SUSPENDED` (tiene uno solo).
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

/// Lanza `cmd` suspendido, sin ventana y en su propio grupo de procesos, lo mete en `job` y lo reanuda.
/// Si algo falla, el hijo se mata: nunca queda un proceso fuera del job.
pub fn spawn_in_job(cmd: &mut Command, job: &Job) -> io::Result<std::process::Child> {
    cmd.creation_flags(CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP);
    let mut child = cmd.spawn()?;
    let setup = job.assign(&child).and_then(|()| resume_process(child.id()));
    if let Err(e) = setup {
        let _ = child.kill();
        let _ = child.wait();
        return Err(e);
    }
    Ok(child)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Stdio;

    #[test]
    fn child_runs_inside_the_job_and_dies_with_it() {
        let job = Job::new().unwrap();
        let mut cmd = Command::new("cmd.exe");
        cmd.args(["/c", "ping -n 30 127.0.0.1 >NUL"]).stdin(Stdio::null()).stdout(Stdio::null());
        let mut child = spawn_in_job(&mut cmd, &job).unwrap();
        assert!(child.try_wait().unwrap().is_none(), "reanudado y en marcha");
        job.terminate();
        let status = child.wait().unwrap();
        assert!(!status.success());
    }
}
