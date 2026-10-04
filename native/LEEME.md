# `native/`

**Dueño:** B1 (B2 pide cambios para el visor).

Código Rust de la v2: espacio de trabajo `Cargo.toml` con `common/` (escritura atómica, ocultación de credenciales y códigos de salida), `vmshost/` (arrancador fijo de los servicios) y `vmsctl/` (anfitrión de procesos y configuración del equipo). El visor Tauri vive aparte en `viewer/` (B2). Toolchain fijado en `rust-toolchain.toml`.

**Referencia:** `cargo test --workspace` y `cargo clippy --workspace --all-targets -- -D warnings` (job B1 de CI). CONTRATO §13-§14.
