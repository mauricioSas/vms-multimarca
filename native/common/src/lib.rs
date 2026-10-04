//! `vms-common`: piezas compartidas por `vmshost`, `vmsctl` y el visor (dueño: B1).
//!
//! - [`atomic`]: escritura atómica (CONTRATO §13.5).
//! - [`redact`]: ocultación de credenciales con los vectores compartidos con Python (CONTRATO §13.6).
//! - [`exit_codes`]: códigos de salida estables de `vmsctl` (CONTRATO §14.2).

pub mod atomic;
pub mod redact;

/// Códigos de salida estables de `vmsctl` (PLAN-V2 §1.3). No se reutilizan ni se cambian.
pub mod exit_codes {
    pub const OK: i32 = 0;
    pub const USAGE: i32 = 2;
    pub const PORT_IN_USE: i32 = 10;
    pub const NO_PERMISSION: i32 = 11;
    pub const HEALTH_FAILED: i32 = 12;
    pub const WINDOWS_ERROR: i32 = 20;
}

pub use atomic::atomic_write;
pub use redact::redact;
