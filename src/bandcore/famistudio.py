"""Las notas de bandcore.notes a un proyecto de texto de FamiStudio, que importan los cuatro juegos.

NESBand y GBBand importan el proyecto directo (`import-song x.txt`); NGBand lo pasa a su chip con
`python -m ngband_tools.famistudio`. Es el mismo formato que escribe audio/compose.py de NESBand y GBBand:

  - Tempo: la rejilla sale de la canción (`estimate_beat`): un tiempo = NoteLength x BeatLength cuadros, y cada patrón
    es un compás de 4 tiempos. Así los charts de cualquier juego caen en los tiempos de la canción.
  - Tiempos de las notas en cuadros dentro de su patrón; una nota puede seguir sonando en el patrón siguiente.
  - Volumen: el formato no lo tiene por nota, así que cada nivel de volumen es un instrumento ("Guitarra 13",
    "Guitarra 9"...) con su envolvente constante. Los juegos eligen el sonido por la palabra del nombre (guitarra,
    teclado, bajo) y el volumen por la envolvente.
  - Batería: en el canal de ruido, con los instrumentos Bombo, Tarola, Platillo y Abierto (las notas y envolventes de
    compose.py). El ruido toca un golpe a la vez: si dos coinciden, gana el que más se oye.
"""

from __future__ import annotations

import math
from pathlib import Path

from .chart import estimate_beat

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
VOLUMES = (15, 13, 11, 9, 7, 5, 3)  # niveles de volumen (cada uno es un instrumento)
BASE = {"Square1": "Guitarra", "Square2": "Teclado", "Triangle": "Bajo"}
DUTY = {"Square1": 1, "Square2": 2}  # 25 % para la guitarra, 50 % para el teclado
# Golpe -> (nota en el ruido, envolvente de volumen), los de compose.py
DRUMS = {
    "Bombo": ("A#2", [15, 12, 9, 6, 3, 1, 0]),
    "Tarola": ("D#3", [13, 11, 8, 6, 4, 2, 1, 0]),
    "Platillo": ("G#3", [7, 4, 2, 0]),
    "Abierto": ("G#3", [9, 7, 6, 5, 4, 3, 2, 1, 0]),
}
ROLE_HIT = {"kick": "Bombo", "snare": "Tarola", "tom1": "Platillo", "tom2": "Abierto", "tom3": "Abierto"}
HIT_PRIORITY = {"Bombo": 0, "Tarola": 1, "Abierto": 2, "Platillo": 3}
BEAT_SPLITS = (4, 3, 2, 5, 6, 7, 1)  # BeatLength a probar: el que divida exacto a los cuadros por tiempo


def note_name(pitch: int) -> str:
    return f"{NAMES[pitch % 12]}{pitch // 12 - 1}"


def level(volume: int) -> int:
    return min(VOLUMES, key=lambda v: abs(v - volume))


def tempo(data: dict) -> tuple[int, int]:
    """(BeatLength, NoteLength) con NoteLength x BeatLength = cuadros por tiempo de la canción."""
    onsets = [n[0] for notes in data["channels"].values() for n in notes] + [h[0] for h in data["drums"]]
    per_beat = estimate_beat(onsets or [0]).frames_per_beat
    beat = next(b for b in BEAT_SPLITS if per_beat % b == 0)
    return beat, per_beat // beat


def drum_notes(drums: list, frames: int) -> list[tuple[int, int, str, str]]:
    """Los golpes como notas del ruido: (cuadro, cuadros, nota, instrumento), uno por cuadro."""
    by_frame: dict[int, str] = {}
    for frame, role, cymbal in drums:
        hit = "Platillo" if cymbal and role not in ("kick", "snare") else ROLE_HIT.get(role, "Tarola")
        if frame not in by_frame or HIT_PRIORITY[hit] < HIT_PRIORITY[by_frame[frame]]:
            by_frame[frame] = hit
    starts = sorted(by_frame)
    out = []
    for i, frame in enumerate(starts):
        hit = by_frame[frame]
        nxt = starts[i + 1] if i + 1 < len(starts) else frames
        out.append((frame, max(1, min(nxt - frame, len(DRUMS[hit][1]))), DRUMS[hit][0], hit))
    return out


def write_project(data: dict, path: Path, author: str = "arreglo") -> dict:
    """Escribe el proyecto. Devuelve un resumen: tempo, compases e instrumentos."""
    beat, note_len = tempo(data)
    rows = 4 * beat  # un compás de 4 tiempos por patrón
    pattern_frames = rows * note_len
    frames = max(1, int(data["frames"]))
    patterns = math.ceil(frames / pattern_frames)

    channels: dict[str, list[tuple[int, int, str, str]]] = {}
    instruments: dict[str, list[str]] = {}
    for name, base in BASE.items():
        notes = []
        for frame, length, pitch, volume in data["channels"].get(name, []):
            if volume <= 0 or frame >= frames:
                continue
            ins = f"{base} {level(volume)}"
            if ins not in instruments:
                envs = [f'\t\t\tEnvelope Type="Volume" Length="1" Values="{level(volume)}"\n']
                if name in DUTY:
                    envs.append(f'\t\t\tEnvelope Type="DutyCycle" Length="1" Values="{DUTY[name]}"\n')
                instruments[ins] = envs
            notes.append((frame, max(1, min(length, frames - frame)), note_name(pitch), ins))
        channels[name] = notes
    channels["Noise"] = drum_notes(data["drums"], frames)
    for hit, (_, env) in DRUMS.items():
        if any(n[3] == hit for n in channels["Noise"]):
            instruments[hit] = [f'\t\t\tEnvelope Type="Volume" Length="{len(env)}" Values="{",".join(map(str, env))}"\n']

    title = (data.get("title") or path.stem).replace('"', "'")
    # (FamiStudio no abre el proyecto si Copyright está vacío)
    out = [f'Project Version="4.5.3" TempoMode="FamiStudio" Name="{title}" Author="{author}" Copyright="{author}"\n']
    for ins, envs in instruments.items():
        out.append(f'\tInstrument Name="{ins}"\n')
        out.extend(envs)
    out.append(f'\tSong Name="{title}" Length="{patterns}" LoopPoint="0" PatternLength="{rows}" BeatLength="{beat}" '
               f'NoteLength="{note_len}" Groove="{note_len}" GroovePaddingMode="Middle"\n')
    for name in ("Square1", "Square2", "Triangle", "Noise", "DPCM"):
        out.append(f'\t\tChannel Type="{name}"\n')
        notes = channels.get(name, [])
        bodies: dict[str, str] = {}
        instances = []
        for p in range(patterns):
            start, end = p * pattern_frames, (p + 1) * pattern_frames
            lines = [f'\t\t\t\t\tNote Time="{frame - start}" Value="{value}" Duration="{max(1, length - 1)}" '
                     f'Instrument="{ins}"\n'
                     for frame, length, value, ins in notes if start <= frame < end]
            if not lines:
                continue
            body = "".join(lines)
            if body not in bodies:
                bodies[body] = f"P{len(bodies) + 1}"
                out.append(f'\t\t\tPattern Name="{bodies[body]}"\n{body}')
            instances.append(f'\t\t\tPatternInstance Time="{p}" Pattern="{bodies[body]}"\n')
        out.extend(instances)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(out), encoding="utf-8", newline="\n")
    return {"beat": beat, "note_length": note_len, "frames_per_beat": beat * note_len, "patterns": patterns,
            "instruments": len(instruments)}
