"""Copy buttons work on a panel opened at http://<lan address>: the browser has no navigator.clipboard there (it is not
a secure context), so every copy goes through app.js copyText, which falls back to a selected textarea."""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"


def test_copy_buttons_go_through_the_fallback():
    app = (WEB / "app.js").read_text(encoding="utf-8")
    assert "async function copyText(" in app and 'execCommand("copy")' in app
    for js in WEB.glob("*.js"):
        if js.name == "app.js":
            continue
        text = js.read_text(encoding="utf-8")
        assert "navigator.clipboard" not in text, f"{js.name}: copy with copyText(), not the clipboard API"


def test_a_shown_token_wraps_on_a_phone():
    settings = (WEB / "settings.js").read_text(encoding="utf-8")
    assert 'h("span", { class: "mono secret" }, apiTokenShown.token)' in settings
    css = (WEB / "style.css").read_text(encoding="utf-8")
    assert re.search(r"\.secret \{[^}]*overflow-wrap: anywhere", css)
