import importlib
import mimetypes

from vms.core import webmime


def test_registry_text_plain_for_js_is_overridden() -> None:
    # Simula un Windows cuyo registro declara .js como text/plain.
    mimetypes.add_type("text/plain", ".js")
    assert mimetypes.guess_type("panel.js")[0] == "text/plain"
    importlib.reload(webmime).ensure_web_mimetypes()
    assert mimetypes.guess_type("panel.js")[0] == "text/javascript"
    assert mimetypes.guess_type("app.css")[0] == "text/css"
