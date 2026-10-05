//! Piezas de vigilancia compartidas por `vmshost` y `vmsctl run`: espera creciente entre relanzamientos
//! y ventana de caídas de la versión a prueba (CONTRATO §13.3 y §14.1).

use std::collections::VecDeque;
use std::time::{Duration, Instant};

/// Espera entre relanzamientos de un hijo que cae (CONTRATO §14.1).
pub const BACKOFF_S: [u64; 5] = [1, 2, 5, 10, 30];
/// Si el hijo aguantó esto en marcha, la espera vuelve a empezar desde la primera.
pub const STABLE_AFTER: Duration = Duration::from_secs(60);
/// Versión a prueba: caídas que provocan la vuelta atrás…
pub const TRIAL_CRASH_LIMIT: usize = 3;
/// …dentro de esta ventana.
pub const TRIAL_CRASH_WINDOW: Duration = Duration::from_secs(600);
/// Plazo para que el actualizador confirme una versión a prueba (30 min).
pub const TRIAL_CONFIRM_TIMEOUT_S: u64 = 1800;

/// Espera creciente: 1, 2, 5, 10 y 30 s; vuelve a empezar tras una ejecución estable.
#[derive(Clone, Debug)]
pub struct Backoff {
    steps: Vec<Duration>,
    step: usize,
    stable: Duration,
}

impl Backoff {
    pub fn new(steps: Vec<Duration>, stable: Duration) -> Self {
        assert!(!steps.is_empty(), "hace falta al menos una espera");
        Self { steps, step: 0, stable }
    }

    /// La del contrato (1, 2, 5, 10, 30 s; estable a los 60 s).
    pub fn standard() -> Self {
        Self::new(BACKOFF_S.iter().map(|s| Duration::from_secs(*s)).collect(), STABLE_AFTER)
    }

    /// Espera antes de relanzar un hijo que cayó tras `ran` en marcha.
    pub fn after_crash(&mut self, ran: Duration) -> Duration {
        if ran >= self.stable {
            self.step = 0;
        }
        let wait = self.steps[self.step.min(self.steps.len() - 1)];
        self.step += 1;
        wait
    }

    pub fn reset(&mut self) {
        self.step = 0;
    }
}

/// Ventana de caídas: «3 caídas en 10 minutos».
#[derive(Clone, Debug)]
pub struct CrashWindow {
    limit: usize,
    window: Duration,
    times: VecDeque<Instant>,
}

impl CrashWindow {
    pub fn new(limit: usize, window: Duration) -> Self {
        Self { limit, window, times: VecDeque::new() }
    }

    pub fn trial() -> Self {
        Self::new(TRIAL_CRASH_LIMIT, TRIAL_CRASH_WINDOW)
    }

    /// Anota una caída y devuelve `true` si ya se alcanzó el límite dentro de la ventana.
    pub fn record(&mut self, at: Instant) -> bool {
        self.times.push_back(at);
        while let Some(&first) = self.times.front() {
            if at.saturating_duration_since(first) > self.window {
                self.times.pop_front();
            } else {
                break;
            }
        }
        self.times.len() >= self.limit
    }

    pub fn count(&self) -> usize {
        self.times.len()
    }

    pub fn reset(&mut self) {
        self.times.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crash_window_counts_only_recent_crashes() {
        let mut w = CrashWindow::trial();
        let t0 = Instant::now();
        assert!(!w.record(t0));
        assert!(!w.record(t0 + Duration::from_secs(700))); // la primera ya salió de la ventana
        assert!(!w.record(t0 + Duration::from_secs(800)));
        assert!(w.record(t0 + Duration::from_secs(900)));
        w.reset();
        assert_eq!(w.count(), 0);
    }

    #[test]
    fn backoff_grows_and_restarts_after_a_stable_run() {
        let mut b = Backoff::standard();
        let quick = Duration::from_millis(200);
        let waits: Vec<u64> = (0..7).map(|_| b.after_crash(quick).as_secs()).collect();
        assert_eq!(waits, [1, 2, 5, 10, 30, 30, 30]);
        assert_eq!(b.after_crash(Duration::from_secs(61)).as_secs(), 1);
        assert_eq!(b.after_crash(quick).as_secs(), 2);
        b.reset();
        assert_eq!(b.after_crash(quick).as_secs(), 1);
    }
}
