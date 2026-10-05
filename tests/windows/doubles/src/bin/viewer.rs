//! Doble del visor `VMS.exe`: abre una ventana (clase `VMSVisorDePrueba`) y espera a que la cierren.
#![cfg_attr(windows, windows_subsystem = "windows")]

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) == Some("--version") {
        println!("VMS-doble {}", env!("CARGO_PKG_VERSION"));
        return;
    }
    let walls = args.iter().any(|a| a == "--walls");
    let title = if walls { "VMS Multimarca (visor de prueba, muros)" } else { "VMS Multimarca (visor de prueba)" };
    #[cfg(windows)]
    std::process::exit(vms_dobles::win::viewer::run(title));
    #[cfg(not(windows))]
    {
        eprintln!("{title}: el visor de prueba solo abre ventanas en Windows");
        std::process::exit(2);
    }
}
