//! `vmsctl version show|switch|confirm|rollback|slot|slot-confirm|slot-rollback` (CONTRATO §13.4, §14.1).
//!
//! Escriben `state\active.json`, así que solo funcionan con permisos de SYSTEM o de Administrador elevado
//! (los usan el actualizador y el instalador). Sin permiso: código 11.

use crate::cli::{CtlError, Outcome};
use crate::ctx::Ctx;
use serde_json::json;
use vms_common::state::{now_unix, validate_slot, validate_version};

pub fn show(ctx: &Ctx) -> Result<Outcome, CtlError> {
    let install = ctx.install()?;
    let st = ctx.data.state();
    let installed = install.installed_versions();
    let resolved = st.resolve(|v| install.version_installed(v));
    let journal = st.read_journal().ok();
    let (pointer, rebuilt, why) = match &resolved {
        Ok(r) => (Some(r.pointer.clone()), r.rebuilt, r.why.clone()),
        Err(e) => (None, false, e.to_string()),
    };
    let text = match &pointer {
        Some(p) => format!(
            "Versión activa: {}{}{} · instaladas: {}",
            p.active,
            if p.trial { " (a prueba)" } else { "" },
            p.previous.as_ref().map(|v| format!(" · anterior: {v}")).unwrap_or_default(),
            installed.join(", ")
        ),
        None => format!("Sin versión activa: {why}"),
    };
    Ok(Outcome::new(
        json!({"pointer": pointer, "rebuilt": rebuilt, "why": why, "installed": installed,
               "last_good": journal.as_ref().and_then(|j| j.get("last_good").cloned()),
               "own_version": ctx.own_version()}),
        text,
    ))
}

fn current(ctx: &Ctx) -> Result<vms_common::state::Pointer, CtlError> {
    let install = ctx.install()?;
    let r = ctx.data.state().resolve(|v| install.version_installed(v))?;
    Ok(r.pointer)
}

pub fn switch(ctx: &Ctx, to: &str) -> Result<Outcome, CtlError> {
    validate_version(to)?;
    let install = ctx.install()?;
    if !install.version_installed(to) {
        return Err(CtlError::usage(format!(
            "la versión {to} no está instalada (falta {})",
            install.version_vmsctl(to).display()
        )));
    }
    let cur = current(ctx)?;
    if cur.active == to && !cur.trial {
        return Ok(Outcome::new(json!({"pointer": cur, "changed": false}), format!("{to} ya es la versión activa.")));
    }
    let p = ctx.data.state().write_pointer(&cur.switched(to, now_unix()))?;
    Ok(Outcome::new(
        json!({"pointer": p, "changed": true}),
        format!("Versión activa: {to} (a prueba; anterior {}). Confírmala con «vmsctl version confirm».", cur.active),
    ))
}

pub fn confirm(ctx: &Ctx) -> Result<Outcome, CtlError> {
    let st = ctx.data.state();
    let p = st.write_pointer(&current(ctx)?.confirmed())?;
    st.set_last_good(&p.active)?;
    Ok(Outcome::new(json!({"pointer": p}), format!("{} confirmada como versión buena.", p.active)))
}

pub fn rollback(ctx: &Ctx) -> Result<Outcome, CtlError> {
    let install = ctx.install()?;
    let st = ctx.data.state();
    let cur = current(ctx)?;
    let to = [cur.previous.clone(), st.last_good().ok()]
        .into_iter()
        .flatten()
        .find(|v| v != &cur.active && install.version_installed(v))
        .ok_or_else(|| {
            CtlError::usage(format!("no hay otra versión instalada a la que volver desde {}", cur.active))
        })?;
    let p = st.write_pointer(&cur.rolled_back(&to))?;
    Ok(Outcome::new(json!({"pointer": p}), format!("Vuelta atrás: {} → {to}.", cur.active)))
}

pub fn slot(ctx: &Ctx, slot: &str) -> Result<Outcome, CtlError> {
    validate_slot(slot)?;
    let install = ctx.install()?;
    if !install.slot_vmsctl(slot).is_file() {
        return Err(CtlError::usage(format!(
            "la ranura {slot} del actualizador está vacía ({})",
            install.slot_vmsctl(slot).display()
        )));
    }
    let cur = current(ctx)?;
    if cur.updater.slot == slot && !cur.updater.trial {
        return Ok(Outcome::new(
            json!({"pointer": cur, "changed": false}),
            format!("La ranura {slot} ya está activa."),
        ));
    }
    let p = ctx.data.state().write_pointer(&cur.slot_switched(slot, now_unix()))?;
    Ok(Outcome::new(json!({"pointer": p, "changed": true}), format!("Actualizador: ranura {slot} a prueba.")))
}

pub fn slot_confirm(ctx: &Ctx) -> Result<Outcome, CtlError> {
    let p = ctx.data.state().write_pointer(&current(ctx)?.slot_confirmed())?;
    Ok(Outcome::new(json!({"pointer": p}), format!("Ranura {} del actualizador confirmada.", p.updater.slot)))
}

pub fn slot_rollback(ctx: &Ctx) -> Result<Outcome, CtlError> {
    let p = ctx.data.state().write_pointer(&current(ctx)?.slot_rolled_back())?;
    Ok(Outcome::new(json!({"pointer": p}), format!("Actualizador: vuelta a la ranura {}.", p.updater.slot)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use vms_common::layout::{Component, InstallLayout};

    fn ctx_with(versions: &[&str], slots: &[&str]) -> (tempfile::TempDir, Ctx) {
        let d = tempfile::tempdir().unwrap();
        let inst = InstallLayout::new(d.path().join("pf"));
        for v in versions {
            std::fs::create_dir_all(inst.version_vmsctl(v).parent().unwrap()).unwrap();
            std::fs::write(inst.version_vmsctl(v), b"").unwrap();
        }
        for s in slots {
            std::fs::create_dir_all(inst.slot_dir(s)).unwrap();
            std::fs::write(inst.slot_vmsctl(s), b"").unwrap();
        }
        let comp = Component::Version { version: versions[0].into(), root: inst.version_dir(versions[0]) };
        let ctx = Ctx::for_tests(&d.path().join("pd"), Some(&inst.root), Some(comp));
        ctx.data.state().set_last_good(versions[0]).unwrap();
        (d, ctx)
    }

    #[test]
    fn switch_confirm_rollback_cycle() {
        let (_d, ctx) = ctx_with(&["2.0.0", "2.1.0"], &[]);
        assert!(switch(&ctx, "9.9.9").is_err());
        assert!(switch(&ctx, "../x").is_err());
        let out = switch(&ctx, "2.1.0").unwrap();
        assert_eq!(out.data["pointer"]["trial"], true);
        let out = rollback(&ctx).unwrap();
        assert_eq!(out.data["pointer"]["active"], "2.0.0");
        switch(&ctx, "2.1.0").unwrap();
        confirm(&ctx).unwrap();
        assert_eq!(ctx.data.state().last_good().unwrap(), "2.1.0");
        let s = show(&ctx).unwrap();
        assert!(s.text.contains("Versión activa: 2.1.0") && s.text.contains("anterior: 2.0.0"), "{}", s.text);
    }

    #[test]
    fn updater_slot_cycle() {
        let (_d, ctx) = ctx_with(&["2.0.0"], &["a", "b"]);
        assert!(slot(&ctx, "c").is_err());
        let out = slot(&ctx, "b").unwrap();
        assert_eq!(out.data["pointer"]["updater"]["slot"], "b");
        assert_eq!(out.data["pointer"]["updater"]["trial"], true);
        slot_rollback(&ctx).unwrap();
        assert_eq!(ctx.data.state().read_pointer().unwrap().updater.slot, "a");
        slot(&ctx, "b").unwrap();
        slot_confirm(&ctx).unwrap();
        assert!(!ctx.data.state().read_pointer().unwrap().updater.trial);
    }
}
