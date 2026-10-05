// Sin consola en Windows (la versión de depuración la conserva para ver el registro).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    vms_viewer::run();
}
