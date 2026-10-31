"""Pruebas de las páginas compartidas del editor de charts."""

import re
import shutil
import subprocess

import pytest

from bandcore import editor

PROFILE = {
    "game": "PruebaBand",
    "cli": "pruebaband",
    "pads": ["←", "↓", "→", "B", "A"],
    "channels": [
        {"id": "pulso", "label": "Pulso", "color": "#58d0ff", "discrete": False},
        {"id": "ruido", "label": "Ruido", "color": "#ffb45e", "discrete": True, "pitchLabel": "tipo"},
    ],
    "drumInstrument": False,
    "exportLabel": "Exportar",
    "exportTitle": "Guarda y exporta",
    "playerTitle": "Toca la canción",
    "import": None,
}
PLAYER = "window.BandPlayer = { FPS: 60, render: async () => ({ audio: new Float32Array(1), stems: [], sampleRate: 44100 }) };"


def scripts(page: str) -> list[str]:
    return re.findall(r"<script>(.*?)</script>", page, re.S)


def test_editor_page_replaces_every_marker():
    page = editor.render_page({"name": "x", "editable": True, "player": True}, PROFILE, PLAYER)
    assert not re.findall(r"__(?:GAME|PROFILE|PLAYER|MENU|DATA)__", page)
    assert "<title>PruebaBand - editor de charts</title>" in page
    assert '"id":"ruido"' in page and "window.BandPlayer" in page and "dlgOpen" in page


def test_home_page_has_no_player():
    page = editor.render_page({"home": True}, PROFILE, PLAYER, editor.HOME_HTML)
    assert "PruebaBand &middot; editor de charts" in page and "BandPlayer = {" not in page
    assert '"import":null' in page


def test_data_cannot_close_the_script():
    page = editor.render_page({"title": "</script><b>x"}, PROFILE, PLAYER)
    assert "</script><b>x" not in page and "<\\/script><b>x" in page


def test_profile_is_checked():
    with pytest.raises(ValueError, match="pads"):
        editor.render_page({}, {**PROFILE, "pads": ["A"]})
    with pytest.raises(ValueError, match="channels"):
        editor.render_page({}, {k: v for k, v in PROFILE.items() if k != "channels"})


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js no está instalado")
def test_page_scripts_are_valid_javascript(tmp_path):
    pages = {
        "editor": editor.render_page({"editable": True, "player": True}, PROFILE, PLAYER),
        "home": editor.render_page({"home": True}, PROFILE, PLAYER, editor.HOME_HTML),
    }
    for name, page in pages.items():
        for i, code in enumerate(scripts(page)):
            path = tmp_path / f"{name}{i}.js"
            path.write_text(code, encoding="utf-8")
            done = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
            assert done.returncode == 0, f"{name} script {i}: {done.stderr}"
