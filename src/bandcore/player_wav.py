"""Renderiza una canción de un juego con el reproductor de su editor ("Escuchar") y la guarda como WAV.

Corre en el entorno del juego (lo llama `band convert --wav` con `uv run` en su repo):

    uv run python -m bandcore.player_wav nesband_tools sf2_guile salida.wav

Toma lo mismo que la página del editor (el payload, /api/player y el JS del reproductor: la emulación del chip más
el adaptador que define `window.BandPlayer`) y lo corre en Node con `player_render.js`. El volumen se normaliza (a
-1 dB, sin subir más de 4 veces) para comparar consolas que suenan muy distinto de fuerte.
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

RENDER_JS = Path(__file__).with_name("player_render.js")


def render(module: str, name: str, wav: Path) -> None:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("hace falta Node.js para renderizar el reproductor del editor (node no está en el PATH)")
    editor = importlib.import_module(f"{module}.editor")
    game = editor.Game()
    song = game.open(name)
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "data.json").write_text(json.dumps(game.payload(song)), encoding="utf-8")
        (folder / "player.json").write_text(json.dumps(game.player(song)), encoding="utf-8")
        (folder / "player.js").write_text(game.player_js(), encoding="utf-8")
        wav.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run([node, str(RENDER_JS), str(folder), str(wav)])
    if result.returncode != 0:
        raise RuntimeError("el reproductor del editor falló en Node")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        print("uso: python -m bandcore.player_wav <modulo>_tools <canción> salida.wav", file=sys.stderr)
        return 2
    try:
        render(args[0], args[1], Path(args[2]).resolve())
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"player_wav: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
