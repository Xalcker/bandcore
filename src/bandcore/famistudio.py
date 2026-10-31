"""Proyectos de texto de FamiStudio, que importan los cuatro juegos, a partir de notas o de una grabación del APU.

NESBand y GBBand importan el proyecto directo (`import-song x.txt`); NGBand lo pasa a su chip con
`python -m ngband_tools.famistudio`. Es el mismo formato que escribe audio/compose.py de NESBand y GBBand.

Dos maneras de armarlo:

  - `write_project`: un arreglo desde el formato de bandcore.notes (las notas de otra consola). Cada canal toma el
    sonido de su papel según el timbre (TIMBRES): A = guitarra, piano y bajo; B = sintetizador, pulsada y bajo (los
    mismos de GENBand, audio/arreglo_fm.py). En el NES y la Game Boy el timbre es el duty y la forma de la envolvente;
    en la Neo Geo, el nombre del instrumento (NGBand elige el patch FM por la palabra: guitarra, piano, sintetizador,
    pluck, bajo). El volumen de cada nota es un instrumento por nivel ("Guitarra 13", "Guitarra 9"...).
  - `write_recording_project`: una canción del NES tal cual sonó (bandcore.nesapu.Recording, de un VGM): cada nota
    lleva su duty y su envolvente de volumen reales (un instrumento por cada combinación distinta).

En los dos, la rejilla de tiempos sale de la canción (`estimate_beat`): un tiempo = NoteLength x BeatLength cuadros, y
cada patrón es un compás de 4 tiempos, así los charts de cualquier juego caen en los tiempos. Los tiempos de las notas
van en cuadros dentro de su patrón; una nota puede seguir sonando en el siguiente.

En el ruido, una nota de FamiStudio (C0 = 1) toca el periodo 15 - (nota mod 16): A#2 es el periodo 12 (grave, bombo),
D#3 el 7 (tarola), G#3 el 2 (platillo). El modo corto del ruido es la envolvente de duty en 1.
"""

from __future__ import annotations

import math
from pathlib import Path

from .chart import estimate_beat

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
VOLUMES = (15, 13, 11, 9, 7, 5, 3)  # niveles de volumen de un arreglo (cada uno es un instrumento)
TONAL = ("Square1", "Square2", "Triangle")
# Timbre -> canal -> (nombre del instrumento, duty, forma de la envolvente: factores del volumen, el último se sostiene)
TIMBRES = {
    "A": {  # más "banda": guitarra con un poco de caída, piano que decae y bajo
        "Square1": ("Guitarra", 1, (1.0, 1.0, 0.95, 0.9, 0.9, 0.85)),
        "Square2": ("Piano", 2, (1.0, 0.9, 0.8, 0.72, 0.65, 0.6, 0.55, 0.5)),
        "Triangle": ("Bajo", None, (1.0,)),
    },
    "B": {  # más cerca del chip: sintetizador sostenido, sonido pulsado corto y bajo
        "Square1": ("Sintetizador", 2, (1.0,)),
        "Square2": ("Pluck", 0, (1.0, 0.8, 0.6, 0.45, 0.35, 0.3)),
        "Triangle": ("Bajo", None, (1.0,)),
    },
}
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
MAX_INSTRUMENTS = 60  # FamiStudio admite 64
ENVELOPE_FRAMES = (32, 16, 8, 4, 2, 1)  # largos de envolvente a probar hasta caber en MAX_INSTRUMENTS


def note_name(pitch: int) -> str:
    return f"{NAMES[pitch % 12]}{pitch // 12 - 1}"


def noise_note(period: int) -> str:
    """La nota de FamiStudio que toca el periodo de ruido `period` (0-15)."""
    value = 32 + (15 - period)  # C0 = 1; cualquier octava sirve
    return note_name(value - 1 + 12)


def level(volume: int) -> int:
    return min(VOLUMES, key=lambda v: abs(v - volume))


def _trim(values: list[int]) -> list[int]:
    """La envolvente sin la cola que repite el último valor (FamiStudio sostiene el último)."""
    values = list(values) or [0]
    while len(values) > 1 and values[-1] == values[-2]:
        values.pop()
    return values


def _envelope(kind: str, values: list[int]) -> str:
    return f'\t\t\tEnvelope Type="{kind}" Length="{len(values)}" Values="{",".join(map(str, values))}"\n'


def _tempo(onsets: list[int]) -> tuple[int, int]:
    """(BeatLength, NoteLength) con NoteLength x BeatLength = cuadros por tiempo de la canción."""
    per_beat = estimate_beat(onsets or [0]).frames_per_beat
    beat = next(b for b in BEAT_SPLITS if per_beat % b == 0)
    return beat, per_beat // beat


def _assemble(path: Path, title: str, author: str, frames: int, instruments: dict[str, list[str]],
              channels: dict[str, list[tuple[int, int, str, str]]]) -> dict:
    """Escribe el proyecto. `channels`: canal -> [(cuadro, cuadros, nota, instrumento)]."""
    onsets = [n[0] for notes in channels.values() for n in notes]
    beat, note_len = _tempo(onsets)
    rows = 4 * beat  # un compás de 4 tiempos por patrón
    pattern_frames = rows * note_len
    frames = max(1, int(frames))
    patterns = math.ceil(frames / pattern_frames)
    title = (title or path.stem).replace('"', "'")
    # (FamiStudio no abre el proyecto si Copyright está vacío)
    out = [f'Project Version="4.5.3" TempoMode="FamiStudio" Name="{title}" Author="{author}" Copyright="{author}"\n']
    for ins, envs in instruments.items():
        out.append(f'\tInstrument Name="{ins}"\n')
        out.extend(envs)
    out.append(f'\tSong Name="{title}" Length="{patterns}" LoopPoint="0" PatternLength="{rows}" BeatLength="{beat}" '
               f'NoteLength="{note_len}" Groove="{note_len}" GroovePaddingMode="Middle"\n')
    for name in ("Square1", "Square2", "Triangle", "Noise", "DPCM"):
        out.append(f'\t\tChannel Type="{name}"\n')
        notes = [n for n in channels.get(name, []) if n[0] < frames]
        bodies: dict[str, str] = {}
        instances = []
        for p in range(patterns):
            start, end = p * pattern_frames, (p + 1) * pattern_frames
            lines = [f'\t\t\t\t\tNote Time="{frame - start}" Value="{value}" '
                     f'Duration="{max(1, min(length, frames - frame) - 1)}" Instrument="{ins}"\n'
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


# ---- Arreglos (desde bandcore.notes) --------------------------------------------------------------------------------

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


def write_project(data: dict, path: Path, author: str = "arreglo", timbre: str = "A") -> dict:
    """Un arreglo de las notas `data` (formato de bandcore.notes) con el timbre A o B. Devuelve un resumen."""
    frames = max(1, int(data["frames"]))
    sounds = TIMBRES[timbre]
    channels: dict[str, list[tuple[int, int, str, str]]] = {}
    instruments: dict[str, list[str]] = {}
    for name in TONAL:
        base, duty, shape = sounds[name]
        notes = []
        for frame, length, pitch, volume in data["channels"].get(name, []):
            if volume <= 0 or frame >= frames:
                continue
            ins = f"{base} {level(volume)}"
            if ins not in instruments:
                envs = [_envelope("Volume", _trim([max(1, round(level(volume) * f)) for f in shape]))]
                if duty is not None:
                    envs.append(_envelope("DutyCycle", [duty]))
                instruments[ins] = envs
            notes.append((frame, max(1, min(length, frames - frame)), note_name(pitch), ins))
        channels[name] = notes
    channels["Noise"] = drum_notes(data["drums"], frames)
    for hit, (_, env) in DRUMS.items():
        if any(n[3] == hit for n in channels["Noise"]):
            instruments[hit] = [_envelope("Volume", env)]
    info = _assemble(path, data.get("title") or "", author, frames, instruments, channels)
    return {**info, "timbre": timbre}


# ---- Una grabación del APU del NES, tal cual ----------------------------------------------------------------------

def write_recording_project(rec, path: Path, title: str, author: str) -> dict:
    """La canción grabada (bandcore.nesapu.Recording) con su duty y su envolvente de volumen nota por nota.

    Cada combinación distinta de duty y envolvente es un instrumento; si pasan de MAX_INSTRUMENTS, las envolventes se
    acortan (32, 16, 8... cuadros: el resto de la nota sostiene el último valor) hasta que quepan. El DPCM no se
    escribe (sus samples no vienen en un proyecto de texto): devuelve cuántos golpes se perdieron."""
    from .nesapu import noise_duration, tonal_notes

    tonal = {name: tonal_notes(rec.tracks[name]) for name in TONAL}
    for limit in ENVELOPE_FRAMES:
        keys: dict[tuple, str] = {}
        channels: dict[str, list[tuple[int, int, str, str]]] = {}

        def instrument(key: tuple, prefix: str) -> str:
            if key not in keys:
                keys[key] = f"{prefix} {sum(1 for k in keys.values() if k.startswith(prefix)) + 1}"
            return keys[key]

        for name in TONAL:
            track = rec.tracks[name]
            notes = []
            for n in tonal[name]:
                if name == "Triangle":
                    ins = instrument(("tri",), "Triangulo")
                else:
                    env = _trim(track.vol[n.frame : n.frame + min(n.duration, limit)])
                    ins = instrument(("pulse", track.duty[n.frame], tuple(env)), "Pulso")
                notes.append((n.frame, n.duration, note_name(n.pitch), ins))
            channels[name] = notes
        noise = []
        for f, period, vol, short in rec.noise_hits:
            duration = noise_duration(rec, f)
            env = _trim(rec.noise_active[f : f + min(duration, limit)] or [vol])
            if env[0] == 0:
                env = [vol] + env[1:]
            noise.append((f, duration, noise_note(period), instrument(("noise", short, tuple(env)), "Ruido")))
        channels["Noise"] = noise
        if len(keys) <= MAX_INSTRUMENTS:
            break
    instruments: dict[str, list[str]] = {}
    for key, ins in keys.items():
        if key[0] == "tri":
            instruments[ins] = []
        else:
            envs = [_envelope("Volume", list(key[2]))]
            if key[0] == "pulse":
                envs.append(_envelope("DutyCycle", [key[1]]))
            elif key[1]:  # ruido en modo corto
                envs.append(_envelope("DutyCycle", [1]))
            instruments[ins] = envs
    info = _assemble(path, title, author, rec.frames, instruments, channels)
    return {**info, "envelope_frames": limit, "dpcm_lost": len(rec.dpcm_hits)}
