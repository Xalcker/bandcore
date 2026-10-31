"""Las notas de una canción en un formato común, para arreglarla en otra consola.

Cada juego saca las notas de sus fuentes (`<juego> export-notes`) y cada juego sabe tocarlas en su chip. El formato
(JSON):

    {"title", "game", "frames",                          frames = duración en cuadros (60 por segundo)
     "channels": {"Square1": [[cuadro, cuadros, altura MIDI, volumen 0-15], ...],    guitarra (la melodía)
                  "Square2": [...],                                                    teclado (la armonía)
                  "Triangle": [...]},                                                  bajo
     "drums": [[cuadro, papel, platillo], ...]}           papel: kick, snare, tom1, tom2 o tom3 (ver bandcore.drums)

Los nombres de los canales son los del NES porque así los usa la IR de NESBand y GBBand, pero lo que importa es el
papel: un VGM de la Genesis pone ahí los canales que eligió para guitarra, teclado y bajo. Un canal es monofónico:
si dos notas se encimen (dos canales de origen en el mismo papel), la siguiente corta a la anterior (`monophonic`).
"""

from __future__ import annotations

import json
from pathlib import Path

ROLES = {"Square1": "guitarra", "Square2": "teclado", "Triangle": "bajo"}
DEFAULT_VOLUME = 13  # para fuentes que no dicen el volumen de cada nota (un VGM de FM)


def monophonic(notes: list[list[int]]) -> list[list[int]]:
    """Las notas en orden y sin encimarse: cada una termina, a más tardar, donde empieza la siguiente."""
    notes = sorted((list(n) for n in notes), key=lambda n: (n[0], -n[2]))
    out: list[list[int]] = []
    for note in notes:
        if out and out[-1][0] == note[0]:
            continue  # dos notas en el mismo cuadro: se queda la más aguda (la melodía)
        if out and out[-1][0] + out[-1][1] > note[0]:
            out[-1][1] = note[0] - out[-1][0]
        out.append(note)
    return [n for n in out if n[1] > 0]


def make(title: str, game: str, frames: int, channels: dict[str, list], drums: list) -> dict:
    """El diccionario del formato, con los canales ya monofónicos."""
    return {"title": title, "game": game, "frames": frames,
            "channels": {name: monophonic(channels.get(name, [])) for name in ROLES},
            "drums": sorted([list(h) for h in drums])}


def write(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8", newline="\n")


def read(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
