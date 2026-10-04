from __future__ import annotations

import pytest

from tools.camsim.rtsp_proxy import dahua_mapper, generic_mapper, hikvision_mapper


@pytest.mark.parametrize("external,internal", [
    ("/Streaming/Channels/101", "/sim/hik1/ch1/main"),
    ("/Streaming/Channels/102", "/sim/hik1/ch1/sub"),
    ("/Streaming/channels/1202/trackID=0", "/sim/hik1/ch12/sub/trackID=0"),
    ("/Streaming/Channels/103", "/sim/hik1/notfound"),
    ("/otra/cosa", "/sim/hik1/notfound"),
])
def test_hikvision_mapping(external: str, internal: str) -> None:
    m = hikvision_mapper("hik1")
    assert m.to_internal(external) == internal
    if "notfound" not in internal:
        assert m.to_external(internal).lower() == external.lower()


@pytest.mark.parametrize("external,internal", [
    ("/cam/realmonitor?channel=1&subtype=0", "/sim/dah1/ch1/main"),
    ("/cam/realmonitor?subtype=1&channel=4", "/sim/dah1/ch4/sub"),
    ("/cam/realmonitor?channel=2&subtype=0/trackID=0", "/sim/dah1/ch2/main/trackID=0"),
    ("/cam/realmonitor?channel=2&subtype=7", "/sim/dah1/notfound"),
    ("/cam/realmonitor?channel=x", "/sim/dah1/notfound"),
])
def test_dahua_mapping(external: str, internal: str) -> None:
    m = dahua_mapper("dah1")
    assert m.to_internal(external) == internal
    if "notfound" not in internal:
        back = m.to_external(internal)
        assert m.to_internal(back) == internal


def test_generic_mapping() -> None:
    m = generic_mapper("gen1")
    assert m.to_internal("/ch3/sub") == "/sim/gen1/ch3/sub"
    assert m.to_external("/sim/gen1/ch3/sub/trackID=1") == "/ch3/sub/trackID=1"
    assert m.to_external("/sim/otro/ch3/sub") == "/sim/otro/ch3/sub"
