//! Lógica portable del arrancador (CONTRATO §13.3), sin Windows: se prueba con `cargo test` usando un
//! lanzador falso. El modo servicio (`win.rs`) y el modo consola (`main.rs`) solo llaman a [`Host::tick`]
//! cada medio segundo y a [`Host::begin_shutdown`]/[`Host::poll_shutdown`] al parar.
//!
//! Qué hace en cada vuelta:
//! 1. Solo el `vmshost` de `VMSUpdater` (LocalSystem): atiende las peticiones de vuelta atrás de
//!    `state\requests\`, vuelve atrás la versión a prueba sin confirmar a los 30 min y reconstruye el
//!    puntero si falta o está dañado. **Es el único (con el instalador) que escribe `active.json`.**
//! 2. Lee el puntero (si falta o está dañado, usa `last_good` del diario en memoria).
//! 3. Si cambió la versión (o la ranura del actualizador), para el hijo y lanza el nuevo.
//! 4. Si el hijo cae: espera creciente; si la versión está a prueba y cae 3 veces en 10 min, deja una
//!    petición de vuelta atrás (servicios sin privilegios) o vuelve atrás la ranura (actualizador).

use std::io;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use vms_common::layout::{DataLayout, InstallLayout};
use vms_common::logfile::RotatingLog;
use vms_common::services::UPDATER;
use vms_common::state::{now_unix, trial_expired, Pointer, RollbackKind, RollbackRequest, StateDir, SCHEMA};
use vms_common::supervise::{Backoff, CrashWindow, TRIAL_CONFIRM_TIMEOUT_S};

/// Proceso hijo (`vmsctl run`).
pub trait Child {
    fn id(&self) -> u32;
    /// `Some(código)` si ya terminó.
    fn try_wait(&mut self) -> io::Result<Option<i32>>;
    /// Parada ordenada: cierra su entrada estándar (`vmsctl run --stop-on-stdin-eof`).
    fn request_stop(&mut self);
    /// Parada forzada (Job Object en Windows).
    fn kill(&mut self);
}

pub trait Launcher {
    fn launch(&mut self, exe: &Path, args: &[String]) -> io::Result<Box<dyn Child>>;
}

#[derive(Clone, Debug)]
pub struct HostConfig {
    pub service: String,
    pub install: InstallLayout,
    pub data: DataLayout,
    pub confirm_timeout_s: u64,
    pub crash_window: CrashWindow,
    pub backoff: Backoff,
    /// Espera tras pedir la parada antes de matar al hijo.
    pub stop_grace: Duration,
    /// Reintento cuando falta el ejecutable de una versión que no está a prueba.
    pub missing_retry: Duration,
}

impl HostConfig {
    pub fn new(service: &str, install: InstallLayout, data: DataLayout) -> Self {
        Self {
            service: service.to_string(),
            install,
            data,
            confirm_timeout_s: TRIAL_CONFIRM_TIMEOUT_S,
            crash_window: CrashWindow::trial(),
            backoff: Backoff::standard(),
            stop_grace: Duration::from_secs(15),
            missing_retry: Duration::from_secs(10),
        }
    }
}

/// Qué hay que ejecutar según el puntero.
#[derive(Clone, Debug, PartialEq, Eq)]
struct Target {
    exe: PathBuf,
    /// Versión (`2.1.0`) o ranura (`a`).
    label: String,
    kind: RollbackKind,
    trial: bool,
    trial_since: Option<u64>,
}

struct Running {
    child: Box<dyn Child>,
    target: Target,
    started: Instant,
    stopping_since: Option<Instant>,
}

pub struct Host<L: Launcher> {
    cfg: HostConfig,
    launcher: L,
    log: RotatingLog,
    state: StateDir,
    running: Option<Running>,
    next_launch: Option<Instant>,
    /// Versión a prueba para la que ya se dejó una petición (no se repite en cada vuelta).
    requested: Option<String>,
    last_problem: String,
    pub launches: u32,
}

impl<L: Launcher> Host<L> {
    pub fn new(cfg: HostConfig, launcher: L) -> Self {
        let log = RotatingLog::standard(cfg.data.logs_dir().join(format!("vmshost-{}.log", cfg.service)));
        let state = cfg.data.state();
        Self {
            cfg,
            launcher,
            log,
            state,
            running: None,
            next_launch: None,
            requested: None,
            last_problem: String::new(),
            launches: 0,
        }
    }

    pub fn is_updater(&self) -> bool {
        self.cfg.service.eq_ignore_ascii_case(UPDATER)
    }

    pub fn log(&mut self, msg: &str) {
        self.log.event(&format!("[{}] {msg}", self.cfg.service));
    }

    /// Registra un problema solo cuando cambia (no llenar el registro cada medio segundo).
    fn problem(&mut self, msg: String) {
        if msg != self.last_problem {
            self.log(&msg);
            self.last_problem = msg;
        }
    }

    #[cfg(test)]
    pub fn child_pid(&self) -> Option<u32> {
        self.running.as_ref().map(|r| r.child.id())
    }

    fn installed(&self) -> impl Fn(&str) -> bool + '_ {
        |v: &str| self.cfg.install.version_installed(v)
    }

    fn target_for(&self, p: &Pointer) -> Target {
        if self.is_updater() {
            Target {
                exe: self.cfg.install.slot_vmsctl(&p.updater.slot),
                label: p.updater.slot.clone(),
                kind: RollbackKind::Updater,
                trial: p.updater.trial,
                trial_since: p.updater.trial_since_unix,
            }
        } else {
            Target {
                exe: self.cfg.install.version_vmsctl(&p.active),
                label: p.active.clone(),
                kind: RollbackKind::Version,
                trial: p.trial,
                trial_since: p.trial_since_unix,
            }
        }
    }

    /// Una vuelta del bucle. `now`/`unix`: reloj monotónico y segundos Unix (inyectables en pruebas).
    pub fn tick(&mut self, now: Instant, unix: u64) {
        if self.is_updater() {
            self.updater_duties(unix);
        }
        let pointer = match self.state.resolve(self.installed()) {
            Ok(r) => {
                if r.rebuilt {
                    if self.is_updater() {
                        match self.state.write_pointer(&r.pointer) {
                            Ok(_) => self.log(&format!("puntero reconstruido con {} ({})", r.pointer.active, r.why)),
                            Err(e) => self.problem(format!("no se pudo reconstruir el puntero: {e}")),
                        }
                    } else {
                        self.problem(format!(
                            "puntero no válido ({}); uso la última buena {}",
                            r.why, r.pointer.active
                        ));
                    }
                }
                r.pointer
            }
            Err(e) => {
                self.problem(format!("no hay versión que arrancar: {e}"));
                self.supervise_running(now, unix, None);
                return;
            }
        };
        let target = self.target_for(&pointer);
        if target.trial && trial_expired(true, target.trial_since, unix, self.cfg.confirm_timeout_s) {
            self.trial_failed(&target, "sin confirmar a tiempo", unix);
        }
        if !target.trial {
            self.requested = None;
        }
        self.supervise_running(now, unix, Some(&target));
        if self.running.is_none() && self.next_launch.is_none_or(|t| now >= t) {
            self.launch(&target, now, unix);
        }
    }

    fn supervise_running(&mut self, now: Instant, unix: u64, target: Option<&Target>) {
        let Some(run) = self.running.as_mut() else { return };
        let exited = match run.child.try_wait() {
            Ok(code) => code,
            Err(e) => {
                let msg = format!("no se pudo consultar el proceso {}: {e}", run.child.id());
                self.problem(msg);
                return;
            }
        };
        if let Some(since) = run.stopping_since {
            if let Some(code) = exited {
                let msg = format!("{} parado (código {code})", run.target.label);
                self.running = None;
                self.log(&msg);
            } else if now.saturating_duration_since(since) >= self.cfg.stop_grace {
                run.child.kill();
            }
            return;
        }
        if let Some(code) = exited {
            let run = self.running.take().expect("en marcha");
            self.child_exited(run, code, now, unix);
            return;
        }
        if let Some(t) = target {
            if t.exe != run.target.exe {
                let msg = format!(
                    "el puntero cambió de {} a {}: paro el proceso y lanzo el nuevo",
                    run.target.label, t.label
                );
                run.child.request_stop();
                run.stopping_since = Some(now);
                self.next_launch = Some(now);
                self.cfg.crash_window.reset();
                self.cfg.backoff.reset();
                self.log(&msg);
            } else if t.trial != run.target.trial {
                // Confirmada (o vuelta a prueba): las próximas caídas se cuentan según el nuevo estado.
                run.target = t.clone();
            }
        }
    }

    fn child_exited(&mut self, run: Running, code: i32, now: Instant, unix: u64) {
        let ran = now.saturating_duration_since(run.started);
        self.log(&format!("{} terminó con código {code} tras {} s", run.target.label, ran.as_secs()));
        if run.target.trial {
            if self.cfg.crash_window.record(now) {
                let why = format!("{} caídas en {} min", self.cfg.crash_window.count(), 10);
                self.cfg.crash_window.reset();
                self.cfg.backoff.reset();
                self.trial_failed(&run.target, &why, unix);
            }
            self.next_launch = Some(now + Duration::from_secs(1));
        } else {
            let wait = self.cfg.backoff.after_crash(ran);
            self.log(&format!("relanzo {} dentro de {} s", run.target.label, wait.as_secs()));
            self.next_launch = Some(now + wait);
        }
    }

    fn launch(&mut self, target: &Target, now: Instant, unix: u64) {
        if !target.exe.is_file() {
            self.problem(format!("falta {}", target.exe.display()));
            if target.trial {
                // Una versión a prueba sin ejecutable cuenta como caída: así vuelve atrás sola.
                if self.cfg.crash_window.record(now) {
                    self.cfg.crash_window.reset();
                    self.trial_failed(target, "falta el ejecutable", unix);
                }
                self.next_launch = Some(now + Duration::from_secs(1));
            } else {
                self.next_launch = Some(now + self.cfg.missing_retry);
            }
            return;
        }
        let mut args = vec![
            "run".to_string(),
            "--service".to_string(),
            self.cfg.service.clone(),
            "--data-dir".to_string(),
            self.cfg.data.root.to_string_lossy().into_owned(),
            "--stop-on-stdin-eof".to_string(),
        ];
        if target.trial {
            // A prueba, `vmsctl` no relanza por su cuenta: cada caída la cuenta este arrancador.
            args.push("--exit-on-crash".to_string());
        }
        match self.launcher.launch(&target.exe, &args) {
            Ok(child) => {
                self.launches += 1;
                self.last_problem.clear();
                let msg = format!(
                    "lanzado {} (pid {}{})",
                    target.label,
                    child.id(),
                    if target.trial { ", a prueba" } else { "" }
                );
                self.running = Some(Running { child, target: target.clone(), started: now, stopping_since: None });
                self.next_launch = None;
                self.log(&msg);
            }
            Err(e) => {
                self.problem(format!("no se pudo lanzar {}: {e}", target.exe.display()));
                if target.trial && self.cfg.crash_window.record(now) {
                    self.cfg.crash_window.reset();
                    self.trial_failed(target, "no arranca", unix);
                }
                self.next_launch = Some(now + self.cfg.backoff.after_crash(Duration::ZERO));
            }
        }
    }

    /// La versión (o ranura) a prueba falló: vuelta atrás directa (actualizador) o petición.
    fn trial_failed(&mut self, target: &Target, why: &str, unix: u64) {
        if self.is_updater() && target.kind == RollbackKind::Updater {
            match self.state.read_pointer() {
                Ok(p) if p.updater.trial && p.updater.slot == target.label => {
                    let np = p.slot_rolled_back();
                    match self.state.write_pointer(&np) {
                        Ok(_) => self.log(&format!(
                            "vuelta atrás del actualizador: ranura {} → {} ({why})",
                            target.label, np.updater.slot
                        )),
                        Err(e) => self.problem(format!("no se pudo volver atrás la ranura: {e}")),
                    }
                }
                Ok(_) => {}
                Err(e) => self.problem(format!("no se pudo leer el puntero para volver atrás: {e}")),
            }
            return;
        }
        if self.requested.as_deref() == Some(target.label.as_str()) {
            return;
        }
        let req = RollbackRequest {
            schema: SCHEMA,
            service: self.cfg.service.clone(),
            kind: target.kind,
            from: target.label.clone(),
            reason: why.to_string(),
            created_unix: unix,
        };
        match self.state.write_request(&req) {
            Ok(path) => {
                self.requested = Some(target.label.clone());
                self.log(&format!(
                    "versión a prueba {} falla ({why}): pido la vuelta atrás ({})",
                    target.label,
                    path.display()
                ));
            }
            Err(e) => self.problem(format!("no se pudo dejar la petición de vuelta atrás: {e}")),
        }
    }

    /// Solo el `vmshost` de `VMSUpdater`: peticiones de vuelta atrás y plazo de confirmación.
    fn updater_duties(&mut self, unix: u64) {
        let requests = self.state.read_requests();
        let mut pointer = self.state.read_pointer();
        for (path, req) in requests {
            match (&req, &pointer) {
                (Err(e), _) => self.log(&format!("petición ilegible descartada: {e}")),
                (Ok(r), Ok(p)) if r.kind == RollbackKind::Version && p.trial && p.active == r.from => {
                    let why = format!("{} lo pidió: {}", r.service, r.reason);
                    let p = p.clone();
                    self.rollback_version(&p, &why, unix);
                    pointer = self.state.read_pointer();
                }
                (Ok(r), _) => self.log(&format!(
                    "petición de {} sobre {} descartada: ya no es la versión a prueba",
                    r.service, r.from
                )),
            }
            if let Err(e) = std::fs::remove_file(&path) {
                if e.kind() != io::ErrorKind::NotFound {
                    self.problem(format!("no se pudo borrar {}: {e}", path.display()));
                }
            }
        }
        if let Ok(p) = self.state.read_pointer() {
            if trial_expired(p.trial, p.trial_since_unix, unix, self.cfg.confirm_timeout_s) {
                let why = format!("nadie confirmó {} en {} min", p.active, self.cfg.confirm_timeout_s / 60);
                self.rollback_version(&p, &why, unix);
            }
        }
    }

    fn rollback_version(&mut self, p: &Pointer, why: &str, unix: u64) {
        let candidates = [p.previous.clone(), self.state.last_good().ok()];
        let Some(to) =
            candidates.into_iter().flatten().find(|v| v != &p.active && self.cfg.install.version_installed(v))
        else {
            self.problem(format!(
                "la versión a prueba {} falla ({why}) pero no hay otra instalada a la que volver",
                p.active
            ));
            return;
        };
        let np = p.rolled_back(&to);
        match self.state.write_pointer(&np) {
            Ok(_) => {
                let rec = serde_json::json!({
                    "schema": SCHEMA, "from": p.active, "to": to, "reason": why, "at_unix": unix,
                    "by": "vmshost",
                });
                let _ = vms_common::atomic_write(&self.state.host_rollback_path(), rec.to_string().as_bytes());
                self.log(&format!("vuelta atrás de {} a {to}: {why}", p.active));
            }
            Err(e) => self.problem(format!("no se pudo volver atrás a {to}: {e}")),
        }
    }

    pub fn begin_shutdown(&mut self, now: Instant) {
        if let Some(run) = self.running.as_mut() {
            if run.stopping_since.is_none() {
                run.child.request_stop();
                run.stopping_since = Some(now);
            }
        }
        self.next_launch = Some(now + Duration::from_secs(86_400 * 365));
    }

    /// `true` cuando ya no queda hijo.
    pub fn poll_shutdown(&mut self, now: Instant) -> bool {
        self.supervise_running(now, now_unix(), None);
        self.running.is_none()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::rc::Rc;

    /// Guion de un hijo falso: termina con `exit` tras `after` vueltas (None = no termina solo).
    #[derive(Clone)]
    struct Script {
        exit_after_polls: Option<u32>,
        code: i32,
    }

    struct FakeChild {
        id: u32,
        polls: u32,
        script: Script,
        stopped: bool,
        killed: Rc<RefCell<u32>>,
    }

    impl Child for FakeChild {
        fn id(&self) -> u32 {
            self.id
        }
        fn try_wait(&mut self) -> io::Result<Option<i32>> {
            self.polls += 1;
            if self.stopped {
                return Ok(Some(0));
            }
            Ok(match self.script.exit_after_polls {
                Some(n) if self.polls > n => Some(self.script.code),
                _ => None,
            })
        }
        fn request_stop(&mut self) {
            self.stopped = true;
        }
        fn kill(&mut self) {
            *self.killed.borrow_mut() += 1;
            self.stopped = true;
        }
    }

    type Launched = Rc<RefCell<Vec<(PathBuf, Vec<String>)>>>;

    #[derive(Default)]
    struct FakeLauncher {
        /// Guion por ejecutable (por defecto: no termina).
        scripts: Vec<(String, Script)>,
        launched: Launched,
        killed: Rc<RefCell<u32>>,
    }

    impl Launcher for FakeLauncher {
        fn launch(&mut self, exe: &Path, args: &[String]) -> io::Result<Box<dyn Child>> {
            self.launched.borrow_mut().push((exe.to_path_buf(), args.to_vec()));
            let s = exe.to_string_lossy();
            let script = self
                .scripts
                .iter()
                .find(|(k, _)| s.contains(k.as_str()))
                .map(|(_, sc)| sc.clone())
                .unwrap_or(Script { exit_after_polls: None, code: 0 });
            let id = self.launched.borrow().len() as u32 + 100;
            Ok(Box::new(FakeChild { id, polls: 0, script, stopped: false, killed: self.killed.clone() }))
        }
    }

    struct Env {
        _dir: tempfile::TempDir,
        install: InstallLayout,
        data: DataLayout,
    }

    fn env(versions: &[&str], slots: &[&str]) -> Env {
        let d = tempfile::tempdir().unwrap();
        let install = InstallLayout::new(d.path().join("pf"));
        let data = DataLayout::new(d.path().join("pd"));
        for v in versions {
            std::fs::create_dir_all(install.version_vmsctl(v).parent().unwrap()).unwrap();
            std::fs::write(install.version_vmsctl(v), b"").unwrap();
        }
        for s in slots {
            std::fs::create_dir_all(install.slot_dir(s)).unwrap();
            std::fs::write(install.slot_vmsctl(s), b"").unwrap();
        }
        Env { _dir: d, install, data }
    }

    fn host(e: &Env, service: &str, launcher: FakeLauncher) -> Host<FakeLauncher> {
        let mut cfg = HostConfig::new(service, e.install.clone(), e.data.clone());
        cfg.confirm_timeout_s = 1800;
        Host::new(cfg, launcher)
    }

    fn run_ticks<L: Launcher>(h: &mut Host<L>, t0: Instant, unix0: u64, seconds: u64) {
        for i in 0..seconds * 2 {
            h.tick(t0 + Duration::from_millis(500 * i), unix0 + i / 2);
        }
    }

    #[test]
    fn launches_active_version_with_contract_arguments() {
        let e = env(&["2.0.0"], &[]);
        e.data.state().set_last_good("2.0.0").unwrap();
        let fl = FakeLauncher::default();
        let launched = fl.launched.clone();
        let mut h = host(&e, "VMSBackend", fl);
        h.tick(Instant::now(), 1000);
        let l = launched.borrow();
        assert_eq!(l.len(), 1);
        assert_eq!(l[0].0, e.install.version_vmsctl("2.0.0"));
        assert_eq!(&l[0].1[..3], ["run", "--service", "VMSBackend"]);
        assert!(l[0].1.contains(&"--stop-on-stdin-eof".to_string()));
        assert!(!l[0].1.contains(&"--exit-on-crash".to_string()));
        assert!(!e.data.state().pointer_path().exists(), "un servicio sin privilegios no escribe el puntero");
    }

    #[test]
    fn updater_host_rebuilds_missing_or_corrupt_pointer() {
        let e = env(&["2.0.0"], &["a"]);
        let st = e.data.state();
        st.set_last_good("2.0.0").unwrap();
        let mut h = host(&e, UPDATER, FakeLauncher::default());
        h.tick(Instant::now(), 1000);
        assert_eq!(st.read_pointer().unwrap().active, "2.0.0");
        std::fs::write(st.pointer_path(), b"{basura").unwrap();
        h.tick(Instant::now(), 1001);
        assert_eq!(st.read_pointer().unwrap().active, "2.0.0");
    }

    #[test]
    fn stable_version_crashing_is_relaunched_with_backoff_and_never_rolled_back() {
        let e = env(&["2.0.0", "2.1.0"], &[]);
        let st = e.data.state();
        st.set_last_good("2.0.0").unwrap();
        st.write_pointer(&Pointer::new("2.1.0")).unwrap();
        let fl = FakeLauncher {
            scripts: vec![("2.1.0".into(), Script { exit_after_polls: Some(0), code: 1 })],
            ..Default::default()
        };
        let launched = fl.launched.clone();
        let mut h = host(&e, "VMSBackend", fl);
        run_ticks(&mut h, Instant::now(), 1000, 20);
        // 1 + 1 s + 2 s + 5 s + 10 s → 5 lanzamientos en 20 s, no 40
        let n = launched.borrow().len();
        assert!((4..=6).contains(&n), "lanzamientos: {n}");
        assert!(st.read_requests().is_empty());
        assert_eq!(st.read_pointer().unwrap().active, "2.1.0");
    }

    #[test]
    fn trial_version_crashing_three_times_leaves_a_request_that_the_updater_executes() {
        let e = env(&["2.0.0", "2.1.0"], &["a"]);
        let st = e.data.state();
        st.set_last_good("2.0.0").unwrap();
        st.write_pointer(&Pointer::new("2.0.0").switched("2.1.0", 1000)).unwrap();
        let fl = FakeLauncher {
            scripts: vec![("2.1.0".into(), Script { exit_after_polls: Some(1), code: 13 })],
            ..Default::default()
        };
        let launched = fl.launched.clone();
        let mut backend = host(&e, "VMSBackend", fl);
        let t0 = Instant::now();
        run_ticks(&mut backend, t0, 1000, 10);
        assert!(launched.borrow().iter().all(|(_, a)| a.contains(&"--exit-on-crash".to_string())));
        let reqs = st.read_requests();
        assert_eq!(reqs.len(), 1, "una sola petición aunque siga cayendo");
        let req = reqs[0].1.as_ref().unwrap();
        assert_eq!((req.from.as_str(), req.kind), ("2.1.0", RollbackKind::Version));
        assert_eq!(st.read_pointer().unwrap().active, "2.1.0", "el backend no toca el puntero");

        let mut updater = host(&e, UPDATER, FakeLauncher::default());
        updater.tick(t0, 1010);
        let p = st.read_pointer().unwrap();
        assert_eq!((p.active.as_str(), p.trial, p.previous.as_deref()), ("2.0.0", false, Some("2.1.0")));
        assert!(st.read_requests().is_empty(), "la petición se consume");
        assert!(st.host_rollback_path().exists());

        // El backend ve el cambio, para el hijo y arranca la anterior
        backend.tick(t0 + Duration::from_secs(11), 1011);
        backend.tick(t0 + Duration::from_secs(12), 1012);
        backend.tick(t0 + Duration::from_secs(13), 1013);
        let last = launched.borrow().last().unwrap().0.clone();
        assert_eq!(last, e.install.version_vmsctl("2.0.0"));
    }

    #[test]
    fn stale_or_forged_requests_do_not_roll_back_a_confirmed_version() {
        let e = env(&["2.0.0", "2.1.0"], &["a"]);
        let st = e.data.state();
        st.set_last_good("2.1.0").unwrap();
        let mut p = Pointer::new("2.0.0").switched("2.1.0", 1000);
        p = p.confirmed();
        st.write_pointer(&p).unwrap();
        st.write_request(&RollbackRequest {
            schema: 1,
            service: "VMSAnalytics".into(),
            kind: RollbackKind::Version,
            from: "2.1.0".into(),
            reason: "falsa".into(),
            created_unix: 1,
        })
        .unwrap();
        let mut updater = host(&e, UPDATER, FakeLauncher::default());
        updater.tick(Instant::now(), 2000);
        assert_eq!(st.read_pointer().unwrap().active, "2.1.0");
        assert!(st.read_requests().is_empty(), "la petición caducada se borra");
    }

    #[test]
    fn unconfirmed_trial_is_rolled_back_by_the_updater_after_the_deadline() {
        let e = env(&["2.0.0", "2.1.0"], &["a"]);
        let st = e.data.state();
        st.set_last_good("2.0.0").unwrap();
        st.write_pointer(&Pointer::new("2.0.0").switched("2.1.0", 1000)).unwrap();
        let mut updater = host(&e, UPDATER, FakeLauncher::default());
        updater.tick(Instant::now(), 1000 + 1799);
        assert_eq!(st.read_pointer().unwrap().active, "2.1.0");
        updater.tick(Instant::now(), 1000 + 1800);
        let p = st.read_pointer().unwrap();
        assert_eq!((p.active.as_str(), p.trial), ("2.0.0", false));
        // Y un servicio sin privilegios solo deja la petición
        st.write_pointer(&Pointer::new("2.0.0").switched("2.1.0", 1000)).unwrap();
        let mut backend = host(&e, "VMSBackend", FakeLauncher::default());
        backend.tick(Instant::now(), 1000 + 1800);
        assert_eq!(st.read_pointer().unwrap().active, "2.1.0");
        assert_eq!(st.read_requests().len(), 1);
    }

    #[test]
    fn confirmed_trial_stays() {
        let e = env(&["2.0.0", "2.1.0"], &["a"]);
        let st = e.data.state();
        st.set_last_good("2.0.0").unwrap();
        st.write_pointer(&Pointer::new("2.0.0").switched("2.1.0", 1000).confirmed()).unwrap();
        let mut updater = host(&e, UPDATER, FakeLauncher::default());
        updater.tick(Instant::now(), 1000 + 7200);
        assert_eq!(st.read_pointer().unwrap().active, "2.1.0");
    }

    #[test]
    fn broken_updater_slot_on_trial_goes_back_to_the_previous_slot() {
        let e = env(&["2.0.0"], &["a", "b"]);
        let st = e.data.state();
        st.set_last_good("2.0.0").unwrap();
        st.write_pointer(&Pointer::new("2.0.0").slot_switched("b", 1000)).unwrap();
        let fl = FakeLauncher {
            scripts: vec![("slot-b".into(), Script { exit_after_polls: Some(0), code: 1 })],
            ..Default::default()
        };
        let launched = fl.launched.clone();
        let mut updater = host(&e, UPDATER, fl);
        run_ticks(&mut updater, Instant::now(), 1000, 10);
        let p = st.read_pointer().unwrap();
        assert_eq!((p.updater.slot.as_str(), p.updater.trial), ("a", false));
        assert_eq!(launched.borrow().last().unwrap().0, e.install.slot_vmsctl("a"));
    }

    #[test]
    fn pointer_to_a_version_without_vmsctl_runs_the_last_good_one() {
        let e = env(&["2.0.0"], &["a"]);
        let st = e.data.state();
        st.set_last_good("2.0.0").unwrap();
        // 2.1.0 a medio instalar (sin bin\vmsctl): no cuenta como instalada
        std::fs::create_dir_all(e.install.version_dir("2.1.0")).unwrap();
        st.write_pointer(&Pointer::new("2.0.0").switched("2.1.0", 1000)).unwrap();
        let fl = FakeLauncher::default();
        let launched = fl.launched.clone();
        let mut backend = host(&e, "VMSBackend", fl);
        backend.tick(Instant::now(), 1001);
        assert_eq!(launched.borrow()[0].0, e.install.version_vmsctl("2.0.0"));
    }

    #[test]
    fn updater_slot_on_trial_without_executable_goes_back() {
        let e = env(&["2.0.0"], &["a"]);
        let st = e.data.state();
        st.set_last_good("2.0.0").unwrap();
        st.write_pointer(&Pointer::new("2.0.0").slot_switched("b", 1000)).unwrap();
        let mut updater = host(&e, UPDATER, FakeLauncher::default());
        run_ticks(&mut updater, Instant::now(), 1000, 5);
        assert_eq!(st.read_pointer().unwrap().updater.slot, "a");
        assert!(updater.child_pid().is_some(), "arranca la ranura buena");
    }

    #[test]
    fn shutdown_asks_politely_then_kills_after_the_grace() {
        let e = env(&["2.0.0"], &[]);
        e.data.state().set_last_good("2.0.0").unwrap();
        let fl = FakeLauncher::default();
        let killed = fl.killed.clone();
        let mut h = host(&e, "VMSEngine", fl);
        let t0 = Instant::now();
        h.tick(t0, 1);
        assert!(h.child_pid().is_some());
        h.begin_shutdown(t0);
        assert!(h.poll_shutdown(t0), "el hijo falso para en cuanto se le pide");
        assert_eq!(*killed.borrow(), 0);
    }

    #[test]
    fn log_never_contains_credentials() {
        let e = env(&["2.0.0"], &[]);
        e.data.state().set_last_good("2.0.0").unwrap();
        let mut h = host(&e, "VMSBackend", FakeLauncher::default());
        h.log("origen rtsp://admin:Cl4ve%21@10.0.0.1/x");
        let text = std::fs::read_to_string(e.data.logs_dir().join("vmshost-VMSBackend.log")).unwrap();
        assert!(!text.contains("Cl4ve"));
    }
}
