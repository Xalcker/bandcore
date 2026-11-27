"""Charts compartidos: el mismo chart en las cuatro consolas.

Una canción se siente igual en NES, Game Boy, Genesis y Neo Geo si las notas que se tocan son las mismas. Por eso el
chart se genera una sola vez, en el juego de origen (NESBand para las canciones incluidas; la consola del VGM en
`band convert`), y los demás lo usan tal cual en vez de generar el suyo desde su propio arreglo. Así también la
batería es la misma aunque cada chip toque la percusión a su manera.

El archivo vive junto a la canción, en src/songs/<canción>/charts.json (como drums.json y edits.json, sobrevive a
`reimport`). Los cuadros cuentan desde que arranca la música, sin la entrada de la autopista: cada juego le suma su
LEAD_IN al cargarlo (180 en la Game Boy, 128 en los demás).

Formato:
    {"version": 1, "source": "NESBand", "music_frames": 4480, "frames_per_beat": 20, "first_beat": 0,
     "charts": {"guitarra/facil": [[0, 1, null, 0, 0], [40, 2, 70, 1, 0], ...], ...}}
Cada nota es [cuadro, carriles, fin de la cola o null, estrella 0/1, platillo 0/1]. Las claves son
"instrumento/dificultad" (guitarra, teclado, bajo, bateria × facil, medio, dificil, experto); un instrumento que
la canción no tiene no aparece.
"""

from __future__ import annotations

import json
from pathlib import Path

from .chart import ChartNote

CHARTS_FILE = "charts.json"
CHARTS_VERSION = 1
INSTRUMENTS = ("guitarra", "teclado", "bajo", "bateria")
DIFFICULTIES = ("facil", "medio", "dificil", "experto")
KEYS = tuple(f"{i}/{d}" for i in INSTRUMENTS for d in DIFFICULTIES)


def dumps(charts: dict[str, list[ChartNote]], lead_in: int, source: str, music_frames: int, frames_per_beat: int,
          first_beat: int) -> str:
    """El texto de charts.json (una línea por chart). `charts` cuenta con la entrada de la autopista `lead_in`;
    `first_beat` es el cuadro del primer tiempo desde que arranca la música."""
    lines = [
        "{",
        f'  "version": {CHARTS_VERSION},',
        f'  "source": {json.dumps(source)},',
        f'  "music_frames": {music_frames},',
        f'  "frames_per_beat": {frames_per_beat},',
        f'  "first_beat": {first_beat},',
        '  "charts": {',
    ]
    items = []
    for key in KEYS:
        if key not in charts:
            continue
        notes = [[n.frame - lead_in, n.mask, None if n.end is None else n.end - lead_in, int(n.star), int(n.cymbal)]
                 for n in charts[key]]
        items.append(f"    {json.dumps(key)}: {json.dumps(notes, separators=(',', ':'))}")
    lines.append(",\n".join(items))
    lines += ["  }", "}", ""]
    return "\n".join(lines)


def save(song_dir: Path, charts: dict[str, list[ChartNote]], lead_in: int, source: str, music_frames: int,
         frames_per_beat: int, first_beat: int) -> Path:
    path = song_dir / CHARTS_FILE
    path.write_text(dumps(charts, lead_in, source, music_frames, frames_per_beat, first_beat), encoding="utf-8",
                    newline="\n")
    return path


class SharedCharts:
    """Lo que trae un charts.json, ya con la entrada de la autopista del juego que lo carga."""

    def __init__(self, data: dict, lead_in: int):
        if data.get("version") != CHARTS_VERSION:
            raise ValueError(f"{CHARTS_FILE}: versión {data.get('version')} (se esperaba {CHARTS_VERSION})")
        self.source: str = data.get("source", "")
        self.music_frames: int = data["music_frames"]
        self.frames_per_beat: int = data["frames_per_beat"]
        self.first_beat: int = data["first_beat"]
        self.lead_in = lead_in
        self.charts: dict[str, list[ChartNote]] = {}
        for key, notes in data["charts"].items():
            if key not in KEYS:
                raise ValueError(f"{CHARTS_FILE}: chart desconocido {key}")
            self.charts[key] = [
                ChartNote(f + lead_in, mask, None if end is None else end + lead_in, bool(star), bool(cym))
                for f, mask, end, star, cym in notes
            ]

    def instruments(self) -> list[str]:
        return [i for i in INSTRUMENTS if any(k.startswith(i + "/") for k in self.charts)]

    def chart(self, instrument: str, difficulty: str) -> list[ChartNote] | None:
        return self.charts.get(f"{instrument}/{difficulty}")


GAMES = ("NESBand", "GBBand", "GENBand", "NGBand")
# La misma canción se llama distinto en algún juego
RENAME = {"nesband_demo": "demo"}


def sync(source_root: Path, targets: list[Path], names: list[str] | None = None) -> list[str]:
    """Copia el charts.json de cada canción del juego de origen a la misma canción de los otros juegos (solo a las
    que ya existen ahí). Devuelve qué se copió; después hay que reimportar cada canción en su juego."""
    done = []
    sources = sorted((source_root / "src" / "songs").glob(f"*/{CHARTS_FILE}"))
    for path in sources:
        name = path.parent.name
        if names and name not in names and RENAME.get(name) not in names:
            continue
        text = path.read_text(encoding="utf-8")
        SharedCharts(json.loads(text), 0)   # que sea válido antes de copiarlo
        for target in targets:
            song_dir = target / "src" / "songs" / RENAME.get(name, name)
            if not song_dir.is_dir():
                continue
            dest = song_dir / CHARTS_FILE
            if dest.exists() and dest.read_text(encoding="utf-8") == text:
                continue
            dest.write_text(text, encoding="utf-8", newline="\n")
            done.append(f"{target.name}/{song_dir.name}")
    return done


def load(song_dir: Path, lead_in: int) -> SharedCharts | None:
    """El charts.json de la canción, o None si no tiene (entonces el juego genera sus charts como siempre)."""
    path = song_dir / CHARTS_FILE
    if not path.exists():
        return None
    return SharedCharts(json.loads(path.read_text(encoding="utf-8")), lead_in)
