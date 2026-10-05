//! `vms-common`: piezas compartidas por `vmshost`, `vmsctl` y el visor (dueño: B1).
//!
//! - [`atomic`]: escritura atómica (CONTRATO §13.5).
//! - [`redact`]: ocultación de credenciales con los vectores compartidos con Python (CONTRATO §13.6).
//! - [`exit_codes`]: códigos de salida estables de `vmsctl` (CONTRATO §14.2).
//! - [`state`]: puntero `active.json`, diario y peticiones de vuelta atrás (CONTRATO §13.3-§13.5).
//! - [`layout`]: carpetas de instalación y de datos (CONTRATO §13.1).
//! - [`services`]: catálogo de servicios y tipos de puesto (CONTRATO §13.2).
//! - [`supervise`]: espera creciente y ventana de caídas.
//! - [`logfile`]: registro con rotación y ocultación de credenciales.
//! - [`sid`]: SID de servicio (`NT SERVICE\…`) y SID bien conocidos.
//! - [`secret`]: archivos de secretos con DPAPI de máquina (mismo formato que `vms.core.winsec`).

pub mod atomic;
pub mod layout;
pub mod logfile;
pub mod redact;
pub mod secret;
pub mod services;
pub mod sid;
pub mod state;
pub mod supervise;

/// Códigos de salida estables de `vmsctl` (PLAN-V2 §1.3). No se reutilizan ni se cambian.
pub mod exit_codes {
    pub const OK: i32 = 0;
    pub const USAGE: i32 = 2;
    pub const PORT_IN_USE: i32 = 10;
    pub const NO_PERMISSION: i32 = 11;
    pub const HEALTH_FAILED: i32 = 12;
    /// `vmsctl run --exit-on-crash`: el proceso del servicio terminó sin que nadie lo pidiera (lo cuenta
    /// `vmshost` para la versión a prueba).
    pub const CHILD_EXITED: i32 = 13;
    pub const WINDOWS_ERROR: i32 = 20;
}

pub use atomic::atomic_write;
pub use redact::redact;
