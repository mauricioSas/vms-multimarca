//! Asignación de muros a monitores (PLAN-V2 §2.3, CONTRATO §17.2).
//!
//! - Clave de un monitor: `nombre|x,y|anchoxalto`, en **píxeles físicos** (no depende del escalado de Windows).
//! - Un muro con clave guardada va a ese monitor. Si ese monitor ya no está, se busca por nombre (si solo hay
//!   uno con ese nombre y está libre: el monitor se movió o cambió de resolución); si no, el muro se oculta y
//!   reaparece cuando vuelve el monitor. Nunca se corre un muro a otro monitor por haber desaparecido uno.
//! - Un muro sin clave guardada va al monitor N (en orden de izquierda a derecha y de arriba abajo) o, si está
//!   ocupado, al primero libre. El llamador guarda la clave para que la asignación no cambie después.
//! - Un monitor tiene como mucho un muro.

use std::collections::HashSet;

use crate::config::WallAssignment;

#[derive(Clone, Debug, PartialEq)]
pub struct MonitorInfo {
    pub name: String,
    /// Posición y tamaño en píxeles físicos (como los da Windows).
    pub x: i32,
    pub y: i32,
    pub width: u32,
    pub height: u32,
    /// Escalado (1.0 = 96 ppp, 1.5 = 150 %…).
    pub scale: f64,
}

impl MonitorInfo {
    pub fn key(&self) -> String {
        format!("{}|{},{}|{}x{}", self.name, self.x, self.y, self.width, self.height)
    }

    /// Tamaño lógico (CSS) que verá la página a pantalla completa.
    pub fn logical_size(&self) -> (f64, f64) {
        let s = if self.scale > 0.0 { self.scale } else { 1.0 };
        (self.width as f64 / s, self.height as f64 / s)
    }
}

/// Nombre de la clave guardada (`nombre|x,y|wxh` → `nombre`).
pub fn key_name(key: &str) -> &str {
    key.split('|').next().unwrap_or("")
}

#[derive(Clone, Debug, PartialEq)]
pub struct Placement {
    pub wall: u8,
    /// `None` = el muro se oculta (su monitor no está conectado o no hay monitores libres).
    pub monitor: Option<MonitorInfo>,
    /// La clave que conviene guardar en `viewer.json` (cambia si se asignó por primera vez o por nombre).
    pub new_key: Option<String>,
}

/// Orden estable: de izquierda a derecha y de arriba abajo.
pub fn ordered(monitors: &[MonitorInfo]) -> Vec<MonitorInfo> {
    let mut v = monitors.to_vec();
    v.sort_by(|a, b| (a.x, a.y, &a.name).cmp(&(b.x, b.y, &b.name)));
    v
}

/// Decide dónde va cada muro pedido (`requested`, en el orden en que se abren).
pub fn plan(monitors: &[MonitorInfo], saved: &[WallAssignment], requested: &[u8]) -> Vec<Placement> {
    let mons = ordered(monitors);
    let mut taken: HashSet<usize> = HashSet::new();
    let mut out: Vec<Placement> =
        requested.iter().map(|&w| Placement { wall: w, monitor: None, new_key: None }).collect();
    let saved_key = |wall: u8| saved.iter().find(|a| a.wall == wall).and_then(|a| a.monitor_key.clone());

    // 1) claves exactas (tienen prioridad sobre cualquier otra regla)
    for p in out.iter_mut() {
        if let Some(key) = saved_key(p.wall) {
            if let Some(i) = mons.iter().position(|m| m.key() == key) {
                if taken.insert(i) {
                    p.monitor = Some(mons[i].clone());
                }
            }
        }
    }
    // 2) clave guardada que ya no existe: mismo nombre, si es único y está libre
    for p in out.iter_mut().filter(|p| p.monitor.is_none()) {
        let Some(key) = saved_key(p.wall) else { continue };
        let name = key_name(&key);
        let same: Vec<usize> = (0..mons.len()).filter(|&i| mons[i].name == name).collect();
        if same.len() == 1 && !taken.contains(&same[0]) {
            let i = same[0];
            taken.insert(i);
            p.monitor = Some(mons[i].clone());
            p.new_key = Some(mons[i].key());
        }
    }
    // 3) muros sin clave: el monitor N si está libre; si no, el primero libre
    for p in out.iter_mut().filter(|p| p.monitor.is_none()) {
        if saved_key(p.wall).is_some() {
            continue; // su monitor no está: se oculta, no se mueve
        }
        let preferred = usize::from(p.wall.saturating_sub(1));
        let pick = if preferred < mons.len() && !taken.contains(&preferred) {
            Some(preferred)
        } else {
            (0..mons.len()).find(|i| !taken.contains(i))
        };
        if let Some(i) = pick {
            taken.insert(i);
            p.monitor = Some(mons[i].clone());
            p.new_key = Some(mons[i].key());
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    fn m(name: &str, x: i32, y: i32, w: u32, h: u32, scale: f64) -> MonitorInfo {
        MonitorInfo { name: name.into(), x, y, width: w, height: h, scale }
    }

    fn fhd(i: i32) -> MonitorInfo {
        m(&format!(r"\\.\DISPLAY{}", i + 1), i * 1920, 0, 1920, 1080, 1.0)
    }

    fn saved(pairs: &[(u8, &str)]) -> Vec<WallAssignment> {
        pairs
            .iter()
            .map(|(w, k)| WallAssignment {
                wall: *w,
                server: "local".into(),
                monitor_key: Some((*k).into()),
                extra: BTreeMap::new(),
            })
            .collect()
    }

    fn where_is(p: &[Placement]) -> Vec<(u8, Option<String>)> {
        p.iter().map(|x| (x.wall, x.monitor.as_ref().map(|m| m.name.clone()))).collect()
    }

    /// Tabla de casos de 1 a 4 monitores sin nada guardado.
    #[test]
    fn one_to_four_monitors_without_saved_keys() {
        type Case<'a> = (usize, &'a [u8], &'a [Option<usize>]);
        let cases: &[Case] = &[
            (1, &[1], &[Some(0)]),
            (1, &[1, 2, 3, 4], &[Some(0), None, None, None]),
            (2, &[1, 2], &[Some(0), Some(1)]),
            (2, &[1, 2, 3, 4], &[Some(0), Some(1), None, None]),
            (3, &[1, 2, 3], &[Some(0), Some(1), Some(2)]),
            (4, &[1, 2, 3, 4], &[Some(0), Some(1), Some(2), Some(3)]),
            (4, &[2], &[Some(1)]),
            (4, &[4, 1], &[Some(3), Some(0)]),
            (2, &[3, 4], &[Some(0), Some(1)]), // muro 3 sin monitor 3: el primero libre
        ];
        for (n, req, expected) in cases {
            let mons: Vec<MonitorInfo> = (0..*n as i32).map(fhd).collect();
            let p = plan(&mons, &[], req);
            let got: Vec<Option<String>> = p.iter().map(|x| x.monitor.as_ref().map(|m| m.name.clone())).collect();
            let want: Vec<Option<String>> = expected.iter().map(|o| o.map(|i| mons[i].name.clone())).collect();
            assert_eq!(got, want, "{n} monitores, muros {req:?}");
            for x in &p {
                assert_eq!(x.new_key.is_some(), x.monitor.is_some(), "una asignación nueva se guarda");
            }
        }
    }

    #[test]
    fn order_is_by_position_not_by_enumeration() {
        // Windows puede enumerar los monitores en cualquier orden; el de la izquierda es el 1
        let mons = vec![fhd(2), fhd(0), m("izq", -1920, 0, 1920, 1080, 1.0), fhd(1)];
        let p = plan(&mons, &[], &[1, 2, 3, 4]);
        assert_eq!(
            where_is(&p),
            vec![
                (1, Some("izq".into())),
                (2, Some(r"\\.\DISPLAY1".into())),
                (3, Some(r"\\.\DISPLAY2".into())),
                (4, Some(r"\\.\DISPLAY3".into()))
            ]
        );
    }

    #[test]
    fn saved_keys_are_respected_even_reversed() {
        let mons: Vec<MonitorInfo> = (0..4).map(fhd).collect();
        let s = saved(&[(1, &mons[3].key()), (2, &mons[2].key()), (3, &mons[1].key()), (4, &mons[0].key())]);
        let p = plan(&mons, &s, &[1, 2, 3, 4]);
        assert_eq!(p[0].monitor.as_ref().unwrap(), &mons[3]);
        assert_eq!(p[3].monitor.as_ref().unwrap(), &mons[0]);
        assert!(p.iter().all(|x| x.new_key.is_none()), "nada que guardar");
    }

    #[test]
    fn disconnection_hides_only_that_wall_and_reconnection_brings_it_back() {
        let all: Vec<MonitorInfo> = (0..4).map(fhd).collect();
        let s = saved(&[(1, &all[0].key()), (2, &all[1].key()), (3, &all[2].key()), (4, &all[3].key())]);
        // se desconecta el monitor 3
        let without3 = vec![all[0].clone(), all[1].clone(), all[3].clone()];
        let p = plan(&without3, &s, &[1, 2, 3, 4]);
        assert_eq!(p[2].monitor, None, "el muro 3 se oculta");
        assert_eq!(p[3].monitor.as_ref().unwrap(), &all[3], "el muro 4 NO se corre al hueco");
        // vuelve
        let p = plan(&all, &s, &[1, 2, 3, 4]);
        assert_eq!(p[2].monitor.as_ref().unwrap(), &all[2]);
    }

    #[test]
    fn same_monitor_with_new_resolution_is_found_by_name() {
        let old = fhd(1);
        let s = saved(&[(2, &old.key())]);
        let changed = m(&old.name, 1920, 0, 3840, 2160, 2.0);
        let p = plan(&[fhd(0), changed.clone()], &s, &[2]);
        assert_eq!(p[0].monitor.as_ref().unwrap(), &changed);
        assert_eq!(p[0].new_key.as_deref(), Some(changed.key().as_str()), "se guarda la clave nueva");
    }

    #[test]
    fn ambiguous_names_never_guess() {
        // dos monitores con el mismo nombre genérico: solo vale la clave exacta
        let a = m("Generic PnP Monitor", 0, 0, 1920, 1080, 1.0);
        let b = m("Generic PnP Monitor", 1920, 0, 1920, 1080, 1.0);
        let s = saved(&[(1, "Generic PnP Monitor|5000,0|1280x1024")]);
        let p = plan(&[a, b], &s, &[1]);
        assert_eq!(p[0].monitor, None);
    }

    #[test]
    fn a_monitor_holds_one_wall_even_with_conflicting_saved_keys() {
        let mons: Vec<MonitorInfo> = (0..2).map(fhd).collect();
        let s = saved(&[(1, &mons[0].key()), (2, &mons[0].key())]);
        let p = plan(&mons, &s, &[1, 2]);
        assert_eq!(p[0].monitor.as_ref().unwrap(), &mons[0]);
        assert_eq!(p[1].monitor, None, "el muro 2 no se monta encima del 1");
    }

    #[test]
    fn different_dpi_uses_physical_pixels() {
        // portátil al 150 % a la izquierda y monitor 4K al 200 % a la derecha
        let laptop = m("Laptop", 0, 0, 2880, 1800, 1.5);
        let k4 = m("4K", 2880, 0, 3840, 2160, 2.0);
        assert_eq!(laptop.key(), "Laptop|0,0|2880x1800");
        assert_eq!(k4.logical_size(), (1920.0, 1080.0));
        assert_eq!(laptop.logical_size(), (1920.0, 1200.0));
        let p = plan(&[k4.clone(), laptop.clone()], &[], &[1, 2]);
        assert_eq!(p[0].monitor.as_ref().unwrap(), &laptop);
        assert_eq!(p[1].monitor.as_ref().unwrap(), &k4);
        // cambiar el escalado de Windows no cambia la clave: el muro sigue en su monitor
        let k4_rescaled = MonitorInfo { scale: 1.75, ..k4.clone() };
        let s = saved(&[(2, &k4.key())]);
        let p = plan(&[laptop, k4_rescaled.clone()], &s, &[2]);
        assert_eq!(p[0].monitor.as_ref().unwrap(), &k4_rescaled);
        assert!(p[0].new_key.is_none());
    }

    #[test]
    fn no_monitors_hides_everything() {
        let p = plan(&[], &[], &[1, 2]);
        assert!(p.iter().all(|x| x.monitor.is_none() && x.new_key.is_none()));
    }

    #[test]
    fn key_name_parsing() {
        assert_eq!(key_name(r"\\.\DISPLAY1|0,0|1920x1080"), r"\\.\DISPLAY1");
        assert_eq!(key_name(""), "");
    }
}
