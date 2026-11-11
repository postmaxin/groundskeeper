"""The page stamps every local script and stylesheet with the UI's version (so no cache outlives a redeploy) and
carries that version, which the status stream also reports (so an open page can reload itself)."""
import asyncio
import re
from types import SimpleNamespace

from app import main


def test_every_local_script_and_stylesheet_is_stamped():
    html = asyncio.run(main.index(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))).body.decode()
    local = re.findall(r'(?:src|href)="(/[^"?#]+\.(?:js|css)(?:\?[^"]*)?)"', html)   # .js / .css only, not .json
    assert local and all(u.endswith(f"?v={main.UI_VERSION}") for u in local), local
    for name in ("app.js", "ops.js", "cameras.js", "imagery.js", "settings.js", "studio.js", "help.js", "style.css"):
        assert f"/{name}?v=" in html
    assert f'<meta name="ui-version" content="{main.UI_VERSION}">' in html


def test_the_version_follows_the_files(tmp_path, monkeypatch):
    (tmp_path / "a.js").write_text("one")
    monkeypatch.setattr(main, "WEB", tmp_path)
    v1 = main._ui_version()
    (tmp_path / "a.js").write_text("two")
    assert main._ui_version() != v1
