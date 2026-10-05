//! Catálogo de servicios de Windows (CONTRATO §13.2) y tipos de puesto (PLAN-V2 §1.4).

/// Qué ejecuta `vmsctl run` para cada servicio.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    /// `engine\mediamtx.exe <datos>\mediamtx\mediamtx.yml`
    Engine,
    /// `runtime\python.exe -m <módulo>`
    Python(&'static str),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Account {
    /// `NT SERVICE\<nombre>` (cuenta virtual, sin contraseña).
    Virtual,
    /// Solo `VMSUpdater` (para y arranca servicios y cambia el puntero).
    LocalSystem,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ServiceDef {
    pub name: &'static str,
    pub display: &'static str,
    pub description: &'static str,
    pub kind: Kind,
    pub account: Account,
    pub delayed_start: bool,
    /// Espera desde «para, por favor» hasta matar el proceso (CONTRATO §14.1: 10 s). MediaMTX no
    /// necesita parada ordenada: sus segmentos fMP4 se cierran parte a parte (≤ 1 s).
    pub grace_s: u64,
}

impl ServiceDef {
    /// Nombre del registro de la salida del proceso en `logs\` (`engine.log` lo sigue el backend).
    pub fn log_name(&self) -> String {
        match self.kind {
            Kind::Engine => "engine.log".to_string(),
            Kind::Python(_) => format!("{}.log", self.name),
        }
    }

    pub fn account_name(&self) -> Option<String> {
        match self.account {
            Account::Virtual => Some(format!("NT SERVICE\\{}", self.name)),
            Account::LocalSystem => None,
        }
    }

    pub fn is_updater(&self) -> bool {
        self.name == UPDATER
    }
}

pub const ENGINE: &str = "VMSEngine";
pub const BACKEND: &str = "VMSBackend";
pub const ANALYTICS: &str = "VMSAnalytics";
pub const HEARTBEAT: &str = "VMSHeartbeat";
pub const CENTRAL: &str = "VMSCentral";
pub const UPDATER: &str = "VMSUpdater";

/// En orden de arranque (se paran al revés).
pub const SERVICES: [ServiceDef; 6] = [
    ServiceDef {
        name: ENGINE,
        display: "VMS Multimarca · Motor de vídeo",
        description: "Graba las cámaras 24/7 y sirve el vídeo en vivo (MediaMTX). Graba aunque el backend esté parado.",
        kind: Kind::Engine,
        account: Account::Virtual,
        delayed_start: false,
        grace_s: 0,
    },
    ServiceDef {
        name: BACKEND,
        display: "VMS Multimarca · Backend",
        description: "Interfaz web, configuración de cámaras, muros, reproducción y API.",
        kind: Kind::Python("vms"),
        account: Account::Virtual,
        delayed_start: true,
        grace_s: 10,
    },
    ServiceDef {
        name: ANALYTICS,
        display: "VMS Multimarca · Analítica",
        description: "Conteo anónimo de personas en puerta y ocupación de la cola de cajas.",
        kind: Kind::Python("analytics"),
        account: Account::Virtual,
        delayed_start: true,
        grace_s: 10,
    },
    ServiceDef {
        name: HEARTBEAT,
        display: "VMS Multimarca · Latido de sede",
        description: "Envía el estado de la sede al panel central.",
        kind: Kind::Python("central.agent"),
        account: Account::Virtual,
        delayed_start: true,
        grace_s: 10,
    },
    ServiceDef {
        name: CENTRAL,
        display: "VMS Multimarca · Panel central",
        description: "Panel central multisede.",
        kind: Kind::Python("central"),
        account: Account::Virtual,
        delayed_start: false,
        grace_s: 10,
    },
    ServiceDef {
        name: UPDATER,
        display: "VMS Multimarca · Actualizaciones",
        description: "Busca, verifica (TUF) e instala las actualizaciones, con vuelta atrás automática.",
        kind: Kind::Python("vms_updater"),
        account: Account::LocalSystem,
        delayed_start: true,
        grace_s: 10,
    },
];

pub fn by_name(name: &str) -> Option<&'static ServiceDef> {
    SERVICES.iter().find(|s| s.name.eq_ignore_ascii_case(name))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Role {
    /// Puesto de control: motor, backend y actualizador.
    Control,
    /// Tienda con analítica: lo anterior + analítica y latido.
    Store,
    /// Panel central.
    Central,
    /// Solo visor (mira otros PC).
    Viewer,
}

impl Role {
    pub fn parse(s: &str) -> Option<Role> {
        match s.to_ascii_lowercase().as_str() {
            "control" => Some(Role::Control),
            "store" => Some(Role::Store),
            "central" => Some(Role::Central),
            "viewer" => Some(Role::Viewer),
            _ => None,
        }
    }

    pub fn as_str(&self) -> &'static str {
        match self {
            Role::Control => "control",
            Role::Store => "store",
            Role::Central => "central",
            Role::Viewer => "viewer",
        }
    }

    /// Servicios del puesto, en orden de arranque.
    pub fn services(&self) -> Vec<&'static ServiceDef> {
        let names: &[&str] = match self {
            Role::Control => &[ENGINE, BACKEND, UPDATER],
            Role::Store => &[ENGINE, BACKEND, ANALYTICS, HEARTBEAT, UPDATER],
            Role::Central => &[CENTRAL, UPDATER],
            Role::Viewer => &[UPDATER],
        };
        SERVICES.iter().filter(|s| names.contains(&s.name)).collect()
    }

    pub fn has(&self, name: &str) -> bool {
        self.services().iter().any(|s| s.name == name)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roles_match_the_plan() {
        let names = |r: Role| r.services().iter().map(|s| s.name).collect::<Vec<_>>();
        assert_eq!(names(Role::Control), [ENGINE, BACKEND, UPDATER]);
        assert_eq!(names(Role::Store), [ENGINE, BACKEND, ANALYTICS, HEARTBEAT, UPDATER]);
        assert_eq!(names(Role::Central), [CENTRAL, UPDATER]);
        assert_eq!(names(Role::Viewer), [UPDATER]);
        assert_eq!(Role::parse("STORE"), Some(Role::Store));
        assert!(Role::parse("tienda").is_none());
    }

    #[test]
    fn accounts_and_logs() {
        assert_eq!(by_name("vmsbackend").unwrap().account_name().as_deref(), Some("NT SERVICE\\VMSBackend"));
        assert_eq!(by_name(UPDATER).unwrap().account_name(), None);
        assert_eq!(by_name(ENGINE).unwrap().log_name(), "engine.log");
        assert_eq!(by_name(BACKEND).unwrap().log_name(), "VMSBackend.log");
        assert!(by_name("Spooler").is_none());
    }
}
