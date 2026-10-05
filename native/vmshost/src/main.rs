//! `vmshost.exe`: arrancador fijo de los servicios (PLAN-V2 §1.3, CONTRATO §13.3). Dueño: B1.
//!
//! ```text
//! vmshost service --name <Servicio>      lo lanza el SCM (ImagePath de todos los servicios)
//! vmshost service --name <S> --foreground   igual, en una consola (pruebas); para al cerrar la entrada
//! vmshost viewer [--walls]                abre el visor de la versión activa (accesos directos)
//! vmshost show                            puntero resuelto en JSON (diagnóstico)
//! vmshost --version
//! ```
//! Opciones comunes: `--data-dir <ruta>` (por defecto `VMS_DATA_DIR` o `%ProgramData%\VMSMultimarca`) e
//! `--install-dir <ruta>` (por defecto, la carpeta padre de `bin\`). Solo lo cambia el instalador completo:
//! es pequeño, sin red ni Python.

mod host;
#[cfg(windows)]
mod win;

use host::{Child, Host, HostConfig, Launcher};
use std::io;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::time::{Duration, Instant};
use vms_common::exit_codes;
use vms_common::layout::{exe_name, resolve_data_dir, DataLayout, InstallLayout};
use vms_common::state::now_unix;

fn arg(args: &[String], name: &str) -> Option<String> {
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1).cloned())
}

fn install_dir(args: &[String]) -> PathBuf {
    if let Some(p) = arg(args, "--install-dir") {
        return PathBuf::from(p);
    }
    std::env::current_exe()
        .ok()
        .and_then(|exe| exe.parent().and_then(Path::parent).map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."))
}

fn layouts(args: &[String]) -> (InstallLayout, DataLayout) {
    let data = resolve_data_dir(arg(args, "--data-dir").as_deref().map(Path::new));
    (InstallLayout::new(install_dir(args)), DataLayout::new(data))
}

fn usage() -> ExitCode {
    eprintln!("uso: vmshost service --name <Servicio> | viewer [--walls] | show | --version");
    ExitCode::from(exit_codes::USAGE as u8)
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("--version") => {
            println!("vmshost {}", env!("CARGO_PKG_VERSION"));
            ExitCode::SUCCESS
        }
        Some("service") => {
            let Some(name) = arg(&args, "--name") else { return usage() };
            if vms_common::services::by_name(&name).is_none() {
                eprintln!("vmshost: servicio desconocido «{name}»");
                return usage();
            }
            let (install, data) = layouts(&args);
            let cfg = HostConfig::new(&name, install, data);
            if args.iter().any(|a| a == "--foreground") {
                return foreground(cfg);
            }
            #[cfg(windows)]
            {
                win::run_service(cfg)
            }
            #[cfg(not(windows))]
            {
                eprintln!("vmshost: el modo servicio solo existe en Windows; usa «--foreground»");
                ExitCode::from(exit_codes::USAGE as u8)
            }
        }
        Some("viewer") => viewer(&args),
        Some("show") => {
            let (install, data) = layouts(&args);
            match data.state().resolve(|v| install.version_installed(v)) {
                Ok(r) => {
                    let out = serde_json::json!({"pointer": r.pointer, "rebuilt": r.rebuilt, "why": r.why});
                    println!("{out}");
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    eprintln!("vmshost: {e}");
                    ExitCode::from(exit_codes::WINDOWS_ERROR as u8)
                }
            }
        }
        _ => usage(),
    }
}

/// Abre el visor de la versión activa (`versions\<X>\viewer\VMS.exe`), sin esperar.
fn viewer(args: &[String]) -> ExitCode {
    let (install, data) = layouts(args);
    let resolved = match data.state().resolve(|v| install.version_installed(v)) {
        Ok(r) => r,
        Err(e) => {
            eprintln!("vmshost: no hay una versión instalada que abrir: {e}");
            return ExitCode::from(exit_codes::WINDOWS_ERROR as u8);
        }
    };
    let exe = install.version_dir(&resolved.pointer.active).join("viewer").join(exe_name("VMS"));
    let mut cmd = Command::new(&exe);
    if args.iter().any(|a| a == "--walls") {
        cmd.arg("--walls");
    }
    match cmd.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn() {
        Ok(_) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("vmshost: no se pudo abrir {}: {e}", exe.display());
            ExitCode::from(exit_codes::WINDOWS_ERROR as u8)
        }
    }
}

/// Lanzador de consola (sin Job Object fuera de Windows: solo para desarrollo y pruebas).
struct StdLauncher {
    #[cfg(windows)]
    inner: win::JobLauncher,
}

#[cfg(not(windows))]
struct StdChild {
    child: std::process::Child,
    stdin: Option<std::process::ChildStdin>,
}

#[cfg(not(windows))]
impl Child for StdChild {
    fn id(&self) -> u32 {
        self.child.id()
    }
    fn try_wait(&mut self) -> io::Result<Option<i32>> {
        Ok(self.child.try_wait()?.map(|s| s.code().unwrap_or(1)))
    }
    fn request_stop(&mut self) {
        self.stdin = None;
    }
    fn kill(&mut self) {
        let _ = self.child.kill();
    }
}

impl Launcher for StdLauncher {
    fn launch(&mut self, exe: &Path, args: &[String]) -> io::Result<Box<dyn Child>> {
        #[cfg(windows)]
        {
            self.inner.launch(exe, args)
        }
        #[cfg(not(windows))]
        {
            let mut child = Command::new(exe)
                .args(args)
                .stdin(Stdio::piped())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()?;
            let stdin = child.stdin.take();
            Ok(Box::new(StdChild { child, stdin }))
        }
    }
}

/// `--foreground`: el mismo bucle que el servicio; para cuando se cierra su entrada estándar.
fn foreground(cfg: HostConfig) -> ExitCode {
    #[cfg(windows)]
    let launcher = match win::JobLauncher::new() {
        Ok(inner) => StdLauncher { inner },
        Err(e) => {
            eprintln!("vmshost: no se pudo crear el Job Object: {e}");
            return ExitCode::from(exit_codes::WINDOWS_ERROR as u8);
        }
    };
    #[cfg(not(windows))]
    let launcher = StdLauncher {};
    let grace = cfg.stop_grace;
    let mut host = Host::new(cfg, launcher);
    host.log(&format!("en marcha en consola (pid {})", std::process::id()));
    let (tx, rx) = std::sync::mpsc::channel::<()>();
    std::thread::spawn(move || {
        let _ = io::copy(&mut io::stdin(), &mut io::sink());
        let _ = tx.send(());
    });
    loop {
        host.tick(Instant::now(), now_unix());
        match rx.recv_timeout(Duration::from_millis(500)) {
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => continue,
            _ => break,
        }
    }
    let t0 = Instant::now();
    host.begin_shutdown(t0);
    while !host.poll_shutdown(Instant::now()) && t0.elapsed() < grace + Duration::from_secs(5) {
        std::thread::sleep(Duration::from_millis(200));
    }
    host.log("parado (consola)");
    ExitCode::SUCCESS
}
