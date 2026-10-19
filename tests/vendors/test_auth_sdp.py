"""Digest RFC 7616 (vectores del RFC), elección del reto más fuerte y lectura tolerante del SDP."""
from __future__ import annotations

import hashlib
import re

import pytest

from vms.vendors import auth as vauth
from vms.vendors.errors import hik_user_check, text_lock_hint
from vms.vendors.rtsp_probe import is_keyframe
from vms.vendors.sdp import parse_sdp

# RFC 7616 §3.9.1: Mufasa / «Circle of Life», GET /dir/index.html
RFC = {"realm": "http-auth@example.org", "nonce": "7ypf/xlj9XXwfDPEoM4URrv/xwf94BcCAzFZH4GiTo0v",
       "opaque": "FQhe/qaU925kfnzjCev0ciny7QMkPqMAFRtzCUYo5tdS",
       "cnonce": "f2/wE4q74E6zIJEtWaHKaf5wv/H5QzzpXusqGemxURZJ"}


@pytest.mark.parametrize(("alg", "expected"), [
    ("MD5", "8ca523f5e9506fed4657c9700eebdbec"),
    ("SHA-256", "753927fa0e85d155564e2e272a28d1802ca10daf4496794697cf8db5856cb6c1"),
])
def test_rfc7616_vectors(alg: str, expected: str) -> None:
    ch = vauth.parse_challenge(f'Digest realm="{RFC["realm"]}", qop="auth, auth-int", algorithm={alg}, '
                               f'nonce="{RFC["nonce"]}", opaque="{RFC["opaque"]}"')
    header = vauth.authorization(ch, "GET", "/dir/index.html", "Mufasa", "Circle of Life", cnonce=RFC["cnonce"])
    assert f'response="{expected}"' in header
    assert "qop=auth" in header and "nc=00000001" in header and f'opaque="{RFC["opaque"]}"' in header
    assert vauth.verify_digest(header, "GET", "Mufasa", "Circle of Life", RFC["realm"])
    assert not vauth.verify_digest(header, "GET", "Mufasa", "otra", RFC["realm"])


def test_userhash_and_sess() -> None:
    ch = vauth.parse_challenge('Digest realm="r", nonce="n", algorithm=SHA-256-sess, qop="auth", userhash=true')
    header = vauth.authorization(ch, "DESCRIBE", "rtsp://h/x", "admin", "Sim#Pass:1@/x")
    assert 'username="admin"' not in header and "userhash=true" in header
    assert vauth.verify_digest(header, "DESCRIBE", "admin", "Sim#Pass:1@/x", "r")


def test_choose_strongest_and_basic_only_when_allowed() -> None:
    headers = ['Basic realm="cam"', 'Digest realm="cam", nonce="a", algorithm=MD5',
               'Digest realm="cam", nonce="b", algorithm=SHA-256, qop="auth"']
    chs = vauth.parse_challenges(headers)
    assert [c.scheme for c in chs] == ["basic", "digest-md5", "digest-sha256"]
    assert vauth.choose(chs).scheme == "digest-sha256"  # type: ignore[union-attr]
    # varios retos en UNA cabecera separados por coma
    one = vauth.parse_challenges(['Digest realm="x", nonce="1", qop="auth", Basic realm="x"'])
    assert [c.scheme for c in one] == ["digest-md5", "basic"]
    basic = vauth.parse_challenges(['Basic realm="cam"'])
    assert vauth.choose(basic) is None and vauth.only_basic(basic)
    assert vauth.choose(basic, allow_basic=True).scheme == "basic"  # type: ignore[union-attr]
    assert vauth.choose(vauth.parse_challenges(['Digest realm="x", nonce="1", algorithm=SHA-512-256'])) is None


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _param_names(header: str) -> set[str]:
    return {m.lower() for m in re.findall(r'(?:^|,)\s*([\w-]+)\s*=', header.partition(" ")[2])}


# Reto RTSP de la cámara del usuario (Hikvision DS-2CD1741FWD-IZ, firmware V5.5.0), con otro número en el realm y
# otro nonce: Digest de RFC 2069 sin qop ni algorithm, con `stale`, y un Basic aparte.
HIK_V55_REALM = "IP Camera(00000)"
HIK_V55_NONCE = "4e6a46434d7a49324e3045354f444e6a"
HIK_V55 = [f'Digest realm="{HIK_V55_REALM}", nonce="{HIK_V55_NONCE}", stale="FALSE"', f'Basic realm="{HIK_V55_REALM}"']


def test_challenge_without_algorithm_nor_qop_gets_a_rfc2069_answer() -> None:
    """8/10, cámara del usuario: «Probar conexión» decía «Usuario o contraseña RTSP incorrectos» con la contraseña
    buena. Su RTSP reta sin `algorithm` y la respuesta llevaba siempre `algorithm=MD5`: con eso la cámara da 401;
    la misma petición sin él, 200. Como curl y ffmpeg, `algorithm` solo va si el reto lo trae."""
    ch = vauth.choose(vauth.parse_challenges(HIK_V55))
    assert ch is not None and ch.scheme == "digest-md5" and ch.qop == () and not ch.stale
    assert ch.algorithm == "" and ch.hash_algorithm == "MD5"
    uri = "rtsp://192.0.2.10:554/Streaming/Channels/101"
    header = vauth.authorization(ch, "DESCRIBE", uri, "admin", "Sim#Pass:1@/x")
    assert "algorithm" not in header
    assert _param_names(header) == {"username", "realm", "nonce", "uri", "response"}
    ha1, ha2 = _md5(f"admin:{HIK_V55_REALM}:Sim#Pass:1@/x"), _md5(f"DESCRIBE:{uri}")
    assert f'response="{_md5(f"{ha1}:{HIK_V55_NONCE}:{ha2}")}"' in header
    assert vauth.verify_digest(header, "DESCRIBE", "admin", "Sim#Pass:1@/x", HIK_V55_REALM)
    assert not vauth.verify_digest(header, "DESCRIBE", "admin", "otra", HIK_V55_REALM)


def test_qop_without_algorithm_matches_rfc2617_example() -> None:
    """RFC 2617 §3.5: reto con qop y sin algorithm. La respuesta del RFC no lleva algorithm."""
    ch = vauth.parse_challenge('Digest realm="testrealm@host.com", qop="auth,auth-int", '
                               'nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093", opaque="5ccc069c403ebaf9f0171e9517f40e41"')
    header = vauth.authorization(ch, "GET", "/dir/index.html", "Mufasa", "Circle Of Life", cnonce="0a4f113b")
    assert 'response="6629fae49393a05397450978507c4ef1"' in header
    assert "qop=auth" in header and "nc=00000001" in header and 'cnonce="0a4f113b"' in header
    assert "algorithm" not in _param_names(header)
    assert vauth.verify_digest(header, "GET", "Mufasa", "Circle Of Life", "testrealm@host.com")


@pytest.mark.parametrize(("challenge", "sent"), [
    ('Digest realm="r", nonce="n", algorithm=MD5', "algorithm=MD5"),
    ('Digest realm="r", nonce="n", algorithm="MD5", qop="auth"', "algorithm=MD5"),
    ('Digest realm="r", nonce="n", algorithm=SHA-256, qop="auth"', "algorithm=SHA-256"),
    ('Digest realm="r", nonce="n", algorithm=MD5-sess, qop="auth"', "algorithm=MD5-sess"),
    ('Digest realm="r", nonce="n", algorithm=SHA-256, qop="auth", userhash=true', "algorithm=SHA-256"),
])
def test_algorithm_is_echoed_when_the_challenge_has_it(challenge: str, sent: str) -> None:
    """Con `algorithm` en el reto se sigue mandando (SHA-256, -sess y userhash como antes): los equipos y los
    simuladores que lo exigen no cambian."""
    header = vauth.authorization(vauth.parse_challenge(challenge), "GET", "/a", "u", "p")
    assert header.count("algorithm=") == 1 and sent in header
    assert vauth.verify_digest(header, "GET", "u", "p", "r")


def test_nonce_count_grows_with_state() -> None:
    ch = vauth.parse_challenge('Digest realm="r", nonce="n", qop="auth"')
    st = vauth.DigestState(ch)
    a = vauth.authorization(ch, "GET", "/a", "u", "p", state=st)
    b = vauth.authorization(ch, "GET", "/b", "u", "p", state=st)
    assert "nc=00000001" in a and "nc=00000002" in b


# --------------------------------------------------------------------------- bloqueo
def test_hik_user_check_lock_and_remaining() -> None:
    locked = ('<?xml version="1.0"?><userCheck version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
              "<statusValue>401</statusValue><lockStatus>lock</lockStatus><unlockTime>1785</unlockTime>"
              "<retryLoginTime>0</retryLoginTime></userCheck>")
    assert hik_user_check(locked) == (True, 30, 0)
    unlocked = locked.replace(">lock<", ">unlock<").replace("1785", "0").replace(
        "<retryLoginTime>0", "<retryLoginTime>3")
    assert hik_user_check(unlocked) == (False, None, 3)
    assert hik_user_check("<ResponseStatus/>") is None


@pytest.mark.parametrize(("text", "locked", "minutes"), [
    ("Error\r\nUser Locked! Please try again after 30 minutes.\r\n", True, 30),
    ("Error\r\nInvalid Authority!\r\n", False, None),
    ("401 Unauthorized (User Locked) remainLockSecond: 600", True, 10),
    ("account is unlocked", False, None),
])
def test_text_lock_hint(text: str, locked: bool, minutes: int | None) -> None:
    assert text_lock_hint(text) == (locked, minutes)


# --------------------------------------------------------------------------- SDP
H264_SDP = ("v=0\r\no=- 0 0 IN IP4 1.2.3.4\r\ns=x\r\nt=0 0\r\na=control:*\r\nm=video 0 RTP/AVP 96\r\n"
            "a=rtpmap:96 H264/90000\r\na=fmtp:96 packetization-mode=1;sprop-parameter-sets=Z0LAHg==,aM4=\r\n"
            "a=control:trackID=1\r\nm=audio 0 RTP/AVP 8\r\na=control:trackID=2\r\n")


def test_sdp_h264_with_static_audio() -> None:
    info = parse_sdp(H264_SDP)
    assert info.video_codec == "H.264" and info.codecs == ["H264", "PCMA"]
    assert info.control_url(info.media[0], "rtsp://cam/Streaming/Channels/101/") == \
        "rtsp://cam/Streaming/Channels/101/trackID=1"


@pytest.mark.parametrize(("sdp", "codec"), [
    (H264_SDP.replace("\r\n", "\n"), "H.264"),                                         # solo LF
    (H264_SDP.replace("a=rtpmap:96 H264/90000\r\n", ""), "H.264"),                    # sin rtpmap (fmtp delata)
    (H264_SDP.replace(";sprop-parameter-sets=Z0LAHg==,aM4=", ""), "H.264"),           # sin sprop
    ("v=0\nm=video 0 RTP/AVP 96\na=rtpmap:96 H265/90000\n", "H.265"),
    ("v=0\nm=video 0 RTP/AVP 96\na=fmtp:96 sprop-vps=QAE=;sprop-sps=QgE=;sprop-pps=RAE=\n", "H.265"),
    ("v=0\nm=video 0 RTP/AVP 26\n", "MJPEG"),                                          # estático, sin rtpmap
    ("v=0\nm=video 0 RTP/AVP 96\n", None),                                             # dinámico sin pistas
])
def test_sdp_tolerant(sdp: str, codec: str | None) -> None:
    assert parse_sdp(sdp).video_codec == codec


def test_sdp_controls_absolute_and_missing() -> None:
    absolute = parse_sdp("v=0\nm=video 0 RTP/AVP 96\na=rtpmap:96 H264/90000\na=control:rtsp://h:554/x/trackID=0\n")
    assert absolute.control_url(absolute.media[0], "rtsp://h:554/x/") == "rtsp://h:554/x/trackID=0"
    none = parse_sdp("v=0\nm=video 0 RTP/AVP 96\na=rtpmap:96 H264/90000\n")
    assert none.control_url(none.media[0], "rtsp://h:554/x") == "rtsp://h:554/x"


def test_keyframe_detection() -> None:
    assert is_keyframe("H.264", bytes([0x65, 1, 2]))                           # IDR
    assert not is_keyframe("H.264", bytes([0x41, 1, 2]))                       # P
    assert is_keyframe("H.264", bytes([28, 0x85, 1]))                          # FU-A inicio de IDR
    assert not is_keyframe("H.264", bytes([28, 0x05, 1]))                      # FU-A sin bit de inicio
    assert is_keyframe("H.264", bytes([24, 0, 2, 0x67, 1, 0, 2, 0x65, 9]))     # STAP-A con IDR
    assert is_keyframe("H.265", bytes([19 << 1, 1, 0]))                        # IDR_W_RADL
    assert not is_keyframe("H.265", bytes([1 << 1, 1, 0]))                     # TRAIL_R
    assert is_keyframe("H.265", bytes([49 << 1, 1, 0x80 | 19, 0]))             # FU inicio de IDR
    assert is_keyframe("MJPEG", b"\x00")
