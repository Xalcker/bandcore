"""Editor de charts compartido: las páginas (editor.html, home.html, menu.html) y cómo se arman.

Cada juego (NESBand, GBBand, GENBand, NGBand) sirve las mismas páginas con su perfil (`PROFILE`, un dict que pasa a
JSON) y el reproductor de su chip:

    PROFILE = {
        "game": "GBBand",                         # título de la página
        "cli": "gbband",                          # el comando de las herramientas (para los mensajes)
        "pads": ["←", "↓", "→", "B", "A"],        # botón del control de cada carril (verde, rojo, amarillo, azul, naranja)
        "channels": [                             # canales del chip, en el orden en que se muestran y en las teclas 1-9, 0, -
            {"id": "Square1", "label": "Square 1", "color": "#58d0ff", "discrete": False},
            {"id": "Noise", "label": "Ruido", "color": "#ffb45e", "discrete": True, "pitchLabel": "tipo"},
        ],
        "drumInstrument": True,                   # la tabla de batería muestra la columna Instrumento
        "exportLabel": "Exportar a la ROM",       # botón y menú de exportar
        "exportTitle": "Guarda y regenera ...",
        "playerTitle": "Toca la canción con ...", # texto del botón Escuchar
        "stems": [["Square1"], ["Noise", "DPCM"]],   # opcional: qué canales suenan en cada audio del reproductor
        "stemScale": 1 / 32767,                   # opcional: escala de esos audios (1 si ya vienen en -1..1)
        "import": {...} o None,                   # el formulario de Nueva canción (ver menu.html), None sin importador
    }

El reproductor (`player_js`) es la emulación del chip más un adaptador que define `window.BandPlayer` (ver editor.html,
sección Escuchar).
"""

from __future__ import annotations

import html
import json
from pathlib import Path

HERE = Path(__file__).parent
EDITOR_HTML = HERE / "editor.html"
HOME_HTML = HERE / "home.html"
MENU_HTML = HERE / "menu.html"

REQUIRED = ("game", "cli", "pads", "channels", "exportLabel", "exportTitle", "playerTitle")


def _json(value) -> str:
    """JSON que se puede meter en un <script> sin cerrarlo."""
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")


def check_profile(profile: dict) -> None:
    missing = [key for key in REQUIRED if key not in profile]
    if missing:
        raise ValueError(f"al perfil del editor le falta: {', '.join(missing)}")
    if len(profile["pads"]) != 5:
        raise ValueError("el perfil del editor necesita el botón de los 5 carriles (pads)")


def render_page(payload: dict, profile: dict, player_js: str = "", template: Path = EDITOR_HTML) -> str:
    """La página del editor (o, con `template=HOME_HTML`, la de inicio sin canción) lista para servir.

    Los datos van al final: así nada de lo insertado antes puede contener otro marcador."""
    check_profile(profile)
    page = template.read_text(encoding="utf-8")
    page = page.replace("__GAME__", html.escape(profile["game"]))
    page = page.replace("__PROFILE__", _json(profile), 1)
    page = page.replace("__PLAYER__", player_js, 1)
    page = page.replace("__MENU__", MENU_HTML.read_text(encoding="utf-8"), 1)
    return page.replace("__DATA__", _json(payload), 1)
