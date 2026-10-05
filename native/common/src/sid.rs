//! SID de servicio (`NT SERVICE\<nombre>` = `S-1-5-80-…`), calculado sin preguntar a Windows.
//!
//! Windows lo deriva del nombre del servicio: SHA-1 del nombre en mayúsculas codificado en UTF-16LE,
//! y los 20 bytes se leen como cinco enteros de 32 bits little-endian (es lo que devuelve
//! `sc showsid` y `LookupAccountName("NT SERVICE\…")`). Calcularlo permite preparar las ACL antes de
//! que exista el servicio y probarlo en cualquier sistema. En Windows, `vmsctl` lo contrasta con
//! `LookupAccountNameW` (prueba de CI).

/// SHA-1 (FIPS 180-4). Solo para derivar SID de servicio: no se usa para nada de seguridad.
pub fn sha1(data: &[u8]) -> [u8; 20] {
    let mut h: [u32; 5] = [0x6745_2301, 0xEFCD_AB89, 0x98BA_DCFE, 0x1032_5476, 0xC3D2_E1F0];
    let mut msg = data.to_vec();
    let bit_len = (data.len() as u64).wrapping_mul(8);
    msg.push(0x80);
    while msg.len() % 64 != 56 {
        msg.push(0);
    }
    msg.extend_from_slice(&bit_len.to_be_bytes());
    for chunk in msg.chunks_exact(64) {
        let mut w = [0u32; 80];
        for (i, word) in chunk.chunks_exact(4).enumerate() {
            w[i] = u32::from_be_bytes([word[0], word[1], word[2], word[3]]);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let [mut a, mut b, mut c, mut d, mut e] = h;
        for (i, wi) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | (!b & d), 0x5A82_7999),
                20..=39 => (b ^ c ^ d, 0x6ED9_EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1B_BCDC),
                _ => (b ^ c ^ d, 0xCA62_C1D6),
            };
            let t = a.rotate_left(5).wrapping_add(f).wrapping_add(e).wrapping_add(k).wrapping_add(*wi);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = t;
        }
        for (x, y) in h.iter_mut().zip([a, b, c, d, e]) {
            *x = x.wrapping_add(y);
        }
    }
    let mut out = [0u8; 20];
    for (i, x) in h.iter().enumerate() {
        out[i * 4..i * 4 + 4].copy_from_slice(&x.to_be_bytes());
    }
    out
}

/// SID del servicio `name` en forma de texto (`S-1-5-80-…`).
pub fn service_sid(name: &str) -> String {
    let upper: Vec<u8> = name.to_uppercase().encode_utf16().flat_map(u16::to_le_bytes).collect();
    let digest = sha1(&upper);
    let parts: Vec<String> =
        digest.chunks_exact(4).map(|c| u32::from_le_bytes([c[0], c[1], c[2], c[3]]).to_string()).collect();
    format!("S-1-5-80-{}", parts.join("-"))
}

/// SID bien conocidos (nunca nombres localizados: «Administradores» cambia con el idioma).
pub mod well_known {
    pub const LOCAL_SYSTEM: &str = "S-1-5-18";
    pub const ADMINISTRATORS: &str = "S-1-5-32-544";
    pub const USERS: &str = "S-1-5-32-545";
    pub const CREATOR_OWNER: &str = "S-1-3-0";
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hex(b: &[u8]) -> String {
        b.iter().map(|x| format!("{x:02x}")).collect()
    }

    #[test]
    fn sha1_known_vectors() {
        assert_eq!(hex(&sha1(b"")), "da39a3ee5e6b4b0d3255bfef95601890afd80709");
        assert_eq!(hex(&sha1(b"abc")), "a9993e364706816aba3e25717850c26c9cd0d89d");
        let long = b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";
        assert_eq!(hex(&sha1(long)), "84983e441c3bd26ebaae4aa1f95129e5e54670f1");
        assert_eq!(hex(&sha1(&[b'a'; 1000])), "291e9a6c66994949b57ba5e650361e98fc36b1ba");
    }

    #[test]
    fn service_sid_matches_windows() {
        // SID publicado de NT SERVICE\TrustedInstaller (el dueño de los archivos de sistema de Windows)
        assert_eq!(service_sid("TrustedInstaller"), "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464");
        assert_eq!(service_sid("trustedinstaller"), service_sid("TRUSTEDINSTALLER"));
        assert!(service_sid("VMSBackend").starts_with("S-1-5-80-"));
        assert_ne!(service_sid("VMSBackend"), service_sid("VMSEngine"));
    }
}
