# `native/`

**Dueño:** B1 (B2 pide cambios para el visor).

Código Rust de la v2: espacio de trabajo `Cargo.toml` con `common/` (estado, escritura atómica, registros,
DPAPI, SID y ocultación de credenciales), `vmshost/` (arrancador fijo de los servicios) y `vmsctl/`
(anfitrión de procesos y configuración del equipo). El visor Tauri vive aparte en `viewer/` (B2). Toolchain
fijado en `rust-toolchain.toml`. `ci/b1_windows_e2e.py` es la prueba en Windows real del job `b1-windows`.

```
cargo test --workspace
cargo clippy --workspace --all-targets -- -D warnings
cargo clippy --workspace --all-targets --target x86_64-pc-windows-msvc -- -D warnings   # código de Windows
cargo deny check licenses bans sources
```

**Referencia:** CONTRATO §13-§14.
