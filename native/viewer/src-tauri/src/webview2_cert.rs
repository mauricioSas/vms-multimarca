//! Opción A de S5 (PLAN-V2 §2.3): manejador de `ServerCertificateErrorDetected` de WebView2.
//!
//! El servidor remoto usa un certificado autofirmado (`vmsctl tls setup`), que WebView2 no puede verificar.
//! En vez de importarlo en el almacén de confianza del usuario (opción B), cada ventana acepta solo el
//! certificado cuya huella SHA-256 es la guardada para ese servidor (`pinning::webview2_allows`); cualquier
//! otro se cancela y la ventana pasa al aviso «El certificado del servidor cambió». La decisión de WebView2 se
//! guarda por host y certificado durante la sesión, así que un cambio de certificado vuelve a pasar por aquí
//! (incluidas las redirecciones del mismo servidor, WebView2Feedback #4575).
//!
//! Además, en los muros se quita el menú contextual por defecto (kiosco).

use std::sync::{Arc, RwLock};

use tauri::webview::PlatformWebview;
use tauri::{AppHandle, Runtime, WebviewWindow};
use webview2_com::Microsoft::Web::WebView2::Win32::{
    ICoreWebView2_14, COREWEBVIEW2_SERVER_CERTIFICATE_ERROR_ACTION_ALWAYS_ALLOW,
    COREWEBVIEW2_SERVER_CERTIFICATE_ERROR_ACTION_CANCEL,
};
use webview2_com::{take_pwstr, ServerCertificateErrorDetectedEventHandler};
use windows_core::{Interface, PWSTR};

use crate::{log, pinning};

type Pins = Arc<RwLock<Vec<(String, String)>>>;

pub fn install<R: Runtime>(window: &WebviewWindow<R>, pins: Pins, kiosk: bool, app: AppHandle<R>, label: String) {
    let res = window.with_webview(move |pv| {
        // SAFETY: llamadas COM en el hilo de la interfaz, sobre el WebView2 vivo de esta ventana.
        if let Err(e) = unsafe { install_on(&pv, pins, kiosk, app, label) } {
            log::error(format!("No se pudo instalar la comprobación de certificados de WebView2: {e}"));
        }
    });
    if let Err(e) = res {
        log::error(format!("with_webview falló: {e}"));
    }
}

/// # Safety
/// Se llama desde `with_webview` (hilo de la interfaz) con el WebView2 de la ventana.
unsafe fn install_on<R: Runtime>(
    pv: &PlatformWebview,
    pins: Pins,
    kiosk: bool,
    app: AppHandle<R>,
    label: String,
) -> windows_core::Result<()> {
    let core = pv.controller().CoreWebView2()?;
    if kiosk {
        core.Settings()?.SetAreDefaultContextMenusEnabled(false)?;
    }
    let core14: ICoreWebView2_14 = core.cast()?;
    let handler = ServerCertificateErrorDetectedEventHandler::create(Box::new(move |_sender, args| {
        let Some(args) = args else { return Ok(()) };
        let mut uri = PWSTR::null();
        args.RequestUri(&mut uri)?;
        let uri = take_pwstr(uri);
        let cert = args.ServerCertificate()?;
        let mut pem = PWSTR::null();
        cert.ToPemEncoding(&mut pem)?;
        let pem = take_pwstr(pem);
        let allowed = pins.read().map(|p| pinning::webview2_allows(&uri, &pem, &p)).unwrap_or(false);
        let action = if allowed {
            COREWEBVIEW2_SERVER_CERTIFICATE_ERROR_ACTION_ALWAYS_ALLOW
        } else {
            COREWEBVIEW2_SERVER_CERTIFICATE_ERROR_ACTION_CANCEL
        };
        args.SetAction(action)?;
        if !allowed {
            let host = tauri::Url::parse(&uri).ok().and_then(|u| u.host_str().map(str::to_string));
            log::warn(format!("Certificado rechazado en «{label}» para {}: no es el fijado", host.unwrap_or_default()));
            let app = app.clone();
            let label = label.clone();
            // vuelve a comprobar el servidor: muestra «El certificado del servidor cambió» con las dos huellas
            std::thread::spawn(move || {
                crate::app::go(&app, &label);
            });
        }
        Ok(())
    }));
    let mut token = 0i64;
    core14.add_ServerCertificateErrorDetected(&handler, &mut token)?;
    Ok(())
}
