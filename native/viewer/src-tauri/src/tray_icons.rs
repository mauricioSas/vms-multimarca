//! Iconos de la bandeja dibujados en memoria (sin archivos): el muro del icono de la aplicación sobre un
//! círculo del color del estado (verde, ámbar, rojo; gris mientras se comprueba).

use crate::health::Level;

pub const SIZE: u32 = 32;

pub fn color(level: Level) -> [u8; 3] {
    match level {
        Level::Ok => [34, 197, 94],
        Level::Degraded => [245, 158, 11],
        Level::Down => [239, 68, 68],
        Level::Unknown => [156, 163, 175],
    }
}

/// RGBA de `SIZE`×`SIZE`, con borde suavizado.
pub fn rgba(level: Level) -> Vec<u8> {
    let [r, g, b] = color(level);
    let n = SIZE as usize;
    let mut out = vec![0u8; n * n * 4];
    let c = (SIZE as f32 - 1.0) / 2.0;
    let radius = SIZE as f32 / 2.0 - 0.5;
    for y in 0..n {
        for x in 0..n {
            let (dx, dy) = (x as f32 - c, y as f32 - c);
            let d = (dx * dx + dy * dy).sqrt();
            let alpha = (radius - d + 0.5).clamp(0.0, 1.0);
            if alpha <= 0.0 {
                continue;
            }
            // cuadrícula 2x2 blanca dentro del círculo (el «muro»)
            let (fx, fy) = (x as f32 / SIZE as f32, y as f32 / SIZE as f32);
            let in_grid = (0.26..0.74).contains(&fx) && (0.30..0.70).contains(&fy);
            let line = in_grid
                && ((fx - 0.26).abs() < 0.05
                    || (fx - 0.74).abs() < 0.05
                    || (fx - 0.50).abs() < 0.035
                    || (fy - 0.30).abs() < 0.05
                    || (fy - 0.70).abs() < 0.05
                    || (fy - 0.50).abs() < 0.035);
            let i = (y * n + x) * 4;
            let px = if line { [255, 255, 255] } else { [r, g, b] };
            out[i..i + 3].copy_from_slice(&px);
            out[i + 3] = (alpha * 255.0) as u8;
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn icons_differ_by_state_and_have_transparent_corners() {
        let ok = rgba(Level::Ok);
        let down = rgba(Level::Down);
        assert_eq!(ok.len(), (SIZE * SIZE * 4) as usize);
        assert_ne!(ok, down);
        assert_eq!(ok[3], 0, "esquina transparente");
        let center_left = ((SIZE / 2 * SIZE + 3) * 4) as usize;
        assert_eq!(&ok[center_left..center_left + 3], &color(Level::Ok));
        assert_eq!(&down[center_left..center_left + 3], &color(Level::Down));
    }
}
