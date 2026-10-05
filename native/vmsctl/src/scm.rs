//! Administrador de servicios (SCM) detrás de un rasgo: el real (windows-service-rs) y uno en memoria
//! para las pruebas (PLAN-V2 §4.5).

use crate::cli::CtlError;
use serde::Serialize;
use std::path::PathBuf;

/// Lo que `vmsctl` registra para cada servicio (CONTRATO §13.2).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SvcSpec {
    pub name: String,
    pub display: String,
    pub description: String,
    /// `<instalación>\bin\vmshost.exe`
    pub image: PathBuf,
    /// `service --name <Servicio>`
    pub args: Vec<String>,
    /// `NT SERVICE\<Servicio>` o `LocalSystem`.
    pub account: String,
    pub delayed: bool,
    /// Variables del bloque `Environment` del servicio (p. ej. `VMS_DATA_DIR`).
    pub env: Vec<(String, String)>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
#[cfg_attr(not(windows), allow(dead_code))]
pub enum SvcState {
    Stopped,
    StartPending,
    StopPending,
    Running,
    Other,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct SvcInfo {
    pub state: SvcState,
    /// Línea de órdenes completa (`ImagePath`).
    pub image_path: String,
    pub account: String,
}

pub trait Scm {
    fn query(&self, name: &str) -> Result<Option<SvcInfo>, CtlError>;
    /// Crea el servicio o actualiza su configuración (idempotente). Devuelve `true` si lo creó.
    fn install(&mut self, spec: &SvcSpec) -> Result<bool, CtlError>;
    fn delete(&mut self, name: &str) -> Result<(), CtlError>;
    fn start(&mut self, name: &str) -> Result<(), CtlError>;
    fn stop(&mut self, name: &str) -> Result<(), CtlError>;
}

#[cfg(windows)]
pub mod win {
    use super::*;
    use std::ffi::OsString;
    use std::time::Duration;
    use windows_service::service::{
        ServiceAccess, ServiceAction, ServiceActionType, ServiceErrorControl, ServiceFailureActions,
        ServiceFailureResetPeriod, ServiceInfo, ServiceStartType, ServiceState, ServiceType,
    };
    use windows_service::service_manager::{ServiceManager, ServiceManagerAccess};

    const ERROR_SERVICE_DOES_NOT_EXIST: i32 = 1060;
    /// Preapagado: parada ordenada del proceso (10 s) + la del arrancador (15 s) + margen.
    const PRESHUTDOWN_TIMEOUT: Duration = Duration::from_secs(30);
    const ERROR_SERVICE_ALREADY_RUNNING: i32 = 1056;
    const ERROR_SERVICE_NOT_ACTIVE: i32 = 1062;

    fn err(e: windows_service::Error, what: &str) -> CtlError {
        match e {
            windows_service::Error::Winapi(io) => CtlError::io(&io, what),
            other => CtlError::windows(format!("{what}: {other}")),
        }
    }

    fn raw(e: &windows_service::Error) -> Option<i32> {
        match e {
            windows_service::Error::Winapi(io) => io.raw_os_error(),
            _ => None,
        }
    }

    pub struct WinScm {
        mgr: ServiceManager,
    }

    impl WinScm {
        /// `write`: hace falta para crear servicios (exige Administrador); para consultar basta con conectar.
        pub fn connect(write: bool) -> Result<Self, CtlError> {
            let access = if write {
                ServiceManagerAccess::CONNECT | ServiceManagerAccess::CREATE_SERVICE
            } else {
                ServiceManagerAccess::CONNECT
            };
            let mgr = ServiceManager::local_computer(None::<&str>, access)
                .map_err(|e| err(e, "no se pudo abrir el administrador de servicios"))?;
            Ok(Self { mgr })
        }

        fn open(
            &self,
            name: &str,
            access: ServiceAccess,
        ) -> Result<Option<windows_service::service::Service>, CtlError> {
            match self.mgr.open_service(name, access) {
                Ok(s) => Ok(Some(s)),
                Err(e) if raw(&e) == Some(ERROR_SERVICE_DOES_NOT_EXIST) => Ok(None),
                Err(e) => Err(err(e, &format!("no se pudo abrir el servicio {name}"))),
            }
        }
    }

    impl Scm for WinScm {
        fn query(&self, name: &str) -> Result<Option<SvcInfo>, CtlError> {
            let Some(s) = self.open(name, ServiceAccess::QUERY_STATUS | ServiceAccess::QUERY_CONFIG)? else {
                return Ok(None);
            };
            let st = s.query_status().map_err(|e| err(e, &format!("estado de {name}")))?;
            let cfg = s.query_config().map_err(|e| err(e, &format!("configuración de {name}")))?;
            let state = match st.current_state {
                ServiceState::Stopped => SvcState::Stopped,
                ServiceState::StartPending => SvcState::StartPending,
                ServiceState::StopPending => SvcState::StopPending,
                ServiceState::Running => SvcState::Running,
                _ => SvcState::Other,
            };
            Ok(Some(SvcInfo {
                state,
                image_path: cfg.executable_path.to_string_lossy().into_owned(),
                account: cfg.account_name.map(|a| a.to_string_lossy().into_owned()).unwrap_or_default(),
            }))
        }

        fn install(&mut self, spec: &SvcSpec) -> Result<bool, CtlError> {
            let info = ServiceInfo {
                name: OsString::from(&spec.name),
                display_name: OsString::from(&spec.display),
                service_type: ServiceType::OWN_PROCESS,
                start_type: ServiceStartType::AutoStart,
                error_control: ServiceErrorControl::Normal,
                executable_path: spec.image.clone(),
                launch_arguments: spec.args.iter().map(OsString::from).collect(),
                dependencies: vec![],
                account_name: Some(OsString::from(&spec.account)),
                account_password: None,
            };
            let access = ServiceAccess::QUERY_STATUS
                | ServiceAccess::QUERY_CONFIG
                | ServiceAccess::CHANGE_CONFIG
                | ServiceAccess::START
                | ServiceAccess::STOP;
            let (svc, created) = match self.open(&spec.name, access)? {
                Some(s) => {
                    s.change_config(&info).map_err(|e| err(e, &format!("no se pudo actualizar {}", spec.name)))?;
                    (s, false)
                }
                None => {
                    let s = self
                        .mgr
                        .create_service(&info, access)
                        .map_err(|e| err(e, &format!("no se pudo crear {}", spec.name)))?;
                    (s, true)
                }
            };
            svc.set_description(&spec.description).map_err(|e| err(e, "descripción del servicio"))?;
            svc.set_delayed_auto_start(spec.delayed).map_err(|e| err(e, "inicio retrasado"))?;
            // Recuperación del SCM: reiniciar a los 1, 5 y 30 s; contador a cero a las 24 h (CONTRATO §13.2).
            let restart =
                |s: u64| ServiceAction { action_type: ServiceActionType::Restart, delay: Duration::from_secs(s) };
            svc.update_failure_actions(ServiceFailureActions {
                reset_period: ServiceFailureResetPeriod::After(Duration::from_secs(86_400)),
                reboot_msg: None,
                command: None,
                actions: Some(vec![restart(1), restart(5), restart(30)]),
            })
            .map_err(|e| err(e, "acciones de recuperación"))?;
            svc.set_failure_actions_on_non_crash_failures(true).map_err(|e| err(e, "recuperación ante fallos"))?;
            // vmshost acepta PRESHUTDOWN: plazo para la parada ordenada al apagar el equipo.
            svc.set_preshutdown_timeout(PRESHUTDOWN_TIMEOUT).map_err(|e| err(e, "plazo de preapagado"))?;
            let env: Vec<String> = spec.env.iter().map(|(k, v)| format!("{k}={v}")).collect();
            crate::winreg::set_multi(&crate::winreg::service_key(&spec.name), "Environment", &env)
                .map_err(|e| CtlError::io(&e, &format!("variables de entorno de {}", spec.name)))?;
            Ok(created)
        }

        fn delete(&mut self, name: &str) -> Result<(), CtlError> {
            let Some(s) = self.open(name, ServiceAccess::DELETE | ServiceAccess::QUERY_STATUS)? else { return Ok(()) };
            s.delete().map_err(|e| err(e, &format!("no se pudo borrar {name}")))
        }

        fn start(&mut self, name: &str) -> Result<(), CtlError> {
            let Some(s) = self.open(name, ServiceAccess::START | ServiceAccess::QUERY_STATUS)? else {
                return Err(CtlError::usage(format!("el servicio {name} no está instalado")));
            };
            match s.start::<&str>(&[]) {
                Ok(()) => Ok(()),
                Err(e) if raw(&e) == Some(ERROR_SERVICE_ALREADY_RUNNING) => Ok(()),
                Err(e) => Err(err(e, &format!("no se pudo arrancar {name}"))),
            }
        }

        fn stop(&mut self, name: &str) -> Result<(), CtlError> {
            let Some(s) = self.open(name, ServiceAccess::STOP | ServiceAccess::QUERY_STATUS)? else { return Ok(()) };
            match s.stop() {
                Ok(_) => Ok(()),
                Err(e) if raw(&e) == Some(ERROR_SERVICE_NOT_ACTIVE) => Ok(()),
                Err(e) => Err(err(e, &format!("no se pudo parar {name}"))),
            }
        }
    }
}

#[cfg(test)]
pub mod fake {
    use super::*;
    use std::collections::BTreeMap;

    /// SCM en memoria: los servicios arrancan y paran en el acto.
    #[derive(Default)]
    pub struct FakeScm {
        pub services: BTreeMap<String, (SvcSpec, SvcState)>,
        /// Servicios «de otro programa» (v1 con WinSW): nombre → (ImagePath, cuenta).
        pub foreign: BTreeMap<String, (String, String, SvcState)>,
        pub ops: Vec<String>,
    }

    impl FakeScm {
        pub fn image_line(spec: &SvcSpec) -> String {
            format!("\"{}\" {}", spec.image.display(), spec.args.join(" "))
        }
    }

    impl Scm for FakeScm {
        fn query(&self, name: &str) -> Result<Option<SvcInfo>, CtlError> {
            if let Some((spec, st)) = self.services.get(name) {
                return Ok(Some(SvcInfo {
                    state: *st,
                    image_path: Self::image_line(spec),
                    account: spec.account.clone(),
                }));
            }
            Ok(self.foreign.get(name).map(|(img, acc, st)| SvcInfo {
                state: *st,
                image_path: img.clone(),
                account: acc.clone(),
            }))
        }
        fn install(&mut self, spec: &SvcSpec) -> Result<bool, CtlError> {
            let existed = self.services.contains_key(&spec.name) || self.foreign.remove(&spec.name).is_some();
            let st = self.services.get(&spec.name).map(|(_, s)| *s).unwrap_or(SvcState::Stopped);
            self.services.insert(spec.name.clone(), (spec.clone(), st));
            self.ops.push(format!("install {}", spec.name));
            Ok(!existed)
        }
        fn delete(&mut self, name: &str) -> Result<(), CtlError> {
            self.services.remove(name);
            self.foreign.remove(name);
            self.ops.push(format!("delete {name}"));
            Ok(())
        }
        fn start(&mut self, name: &str) -> Result<(), CtlError> {
            let (_, st) = self.services.get_mut(name).ok_or_else(|| CtlError::usage(format!("{name} no existe")))?;
            *st = SvcState::Running;
            self.ops.push(format!("start {name}"));
            Ok(())
        }
        fn stop(&mut self, name: &str) -> Result<(), CtlError> {
            if let Some((_, st)) = self.services.get_mut(name) {
                *st = SvcState::Stopped;
            }
            if let Some((_, _, st)) = self.foreign.get_mut(name) {
                *st = SvcState::Stopped;
            }
            self.ops.push(format!("stop {name}"));
            Ok(())
        }
    }
}
