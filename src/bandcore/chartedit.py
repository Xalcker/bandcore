"""Lógica del editor de charts, sin interfaz: aplicar ediciones manuales y validar (la misma en los cuatro juegos).

Las ediciones viven en src/songs/<canción>/edits.json, aparte del chart que genera el importador.
`<juego> import-song` / `reimport` genera el chart automático de cada instrumento y dificultad y
luego le aplica las ediciones (apply_edits), así que mejorar el generador no pierde las correcciones.
Leer los charts ya exportados (chart.s del NES, .bin de la Game Boy) es propio de cada juego y sigue en su repo.

Formato de edits.json:
    {"version": 1,
     "charts": {"guitarra/facil": [
         {"op": "delete", "frame": 168},
         {"op": "add", "frame": 200, "mask": 5, "end": 230, "star": false},
         {"op": "set", "frame": 328, "mask": 2, "end": null, "star": true}]}}
Las notas se identifican por su cuadro (el mismo del chart, contando la entrada de la autopista).
Si el generador cambia y una operación ya no encuentra su nota, se avisa y se omite.
"""

from __future__ import annotations

import json
from pathlib import Path

from .chart import (  # noqa: F401  (ChartNote y lanes_clash también se usan como chartedit.ChartNote)
    BLOCKED_PAIRS,
    DRUM_BLOCKED_PAIRS,
    STAR_PHRASE_LEN,
    ChartNote,
    check_playable,
    lanes_clash,
)

EDITS_FILE = "edits.json"
EDITS_VERSION = 1
LANE_MASK = 0x1F
KICK_MASK = 0b10000  # el bombo (naranja)
MAX_DRUM_HANDS = 2  # golpes de mano a la vez en la batería: solo hay dos manos
MAX_DRUM_BUTTONS = 3  # botones a la vez: el bombo y dos golpes de mano
TOM_MASK = 0b01101  # carriles de los toms de la batería (verde, amarillo y azul): los únicos que pueden ser platillos


def lanes_of(mask: int) -> set[int]:
    return {b for b in range(5) if mask >> b & 1}


# --- Ediciones ----------------------------------------------------------------------


def load_edits(song_dir: Path) -> dict:
    path = song_dir / EDITS_FILE
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != EDITS_VERSION:
        raise ValueError(f"{path}: versión de edits.json no soportada ({data.get('version')})")
    return data.get("charts", {})


def dump_edits(edits: dict) -> str:
    """El texto de edits.json: una operación por renglón, para que git muestre bien los cambios."""
    edits = {key: ops for key, ops in sorted(edits.items()) if ops}
    if not edits:
        return ""
    lines = ['{', f'  "version": {EDITS_VERSION},', '  "charts": {']
    blocks = []
    for key, ops in edits.items():
        body = ",\n".join("      " + json.dumps(op, ensure_ascii=False) for op in ops)
        blocks.append(f'    {json.dumps(key)}: [\n{body}\n    ]')
    lines.append(",\n".join(blocks))
    lines += ["  }", "}", ""]
    return "\n".join(lines)


def save_edits(song_dir: Path, edits: dict) -> None:
    """Guarda las ediciones; sin ninguna, borra edits.json."""
    path = song_dir / EDITS_FILE
    text = dump_edits(edits)
    if text:
        path.write_text(text, encoding="utf-8", newline="\n")
    elif path.exists():
        path.unlink()


def _note_dict(note: ChartNote) -> dict:
    return {"frame": note.frame, "mask": note.mask, "end": note.end, "star": note.star, "cymbal": note.cymbal}


def apply_edits(chart: list[ChartNote], ops: list[dict], warn=None) -> list[ChartNote]:
    """El chart automático con las ediciones manuales encima. `warn(texto)` recibe lo que no se pudo aplicar."""
    warn = warn or (lambda text: None)
    notes = {n.frame: n for n in chart}
    for op in ops:
        frame, kind = op["frame"], op["op"]
        if kind == "add":
            if frame in notes:
                warn(f"cuadro {frame}: ya hay una nota; se reemplaza con la agregada a mano")
            notes[frame] = ChartNote(frame, op["mask"], op.get("end"), op.get("star", False), op.get("cymbal", False))
        elif frame not in notes:
            warn(f"cuadro {frame}: la nota que se editó ya no existe en el chart automático ({kind} omitido)")
        elif kind == "delete":
            del notes[frame]
        elif kind == "set":
            old = notes[frame]
            notes[frame] = ChartNote(
                frame,
                op.get("mask", old.mask),
                op["end"] if "end" in op else old.end,
                op.get("star", old.star),
                op.get("cymbal", old.cymbal),
            )
        else:
            raise ValueError(f"operación desconocida en edits.json: {kind!r}")
    return sorted(notes.values(), key=lambda n: n.frame)


def diff_charts(auto: list[ChartNote], edited: list[ChartNote]) -> list[dict]:
    """Las operaciones que convierten el chart automático en el editado (inverso de apply_edits)."""
    before = {n.frame: n for n in auto}
    after = {n.frame: n for n in edited}
    ops: list[dict] = []
    for frame in sorted(before.keys() | after.keys()):
        old, new = before.get(frame), after.get(frame)
        if new is None:
            ops.append({"op": "delete", "frame": frame})
        elif old is None:
            ops.append({"op": "add", **{k: v for k, v in _note_dict(new).items() if k != "cymbal" or v}})
        elif old != new:
            changes = {k: v for k, v in _note_dict(new).items() if _note_dict(old)[k] != v}
            ops.append({"op": "set", "frame": frame, **changes})
    return ops


# --- Validación ---------------------------------------------------------------------


def chart_problems(chart: list[ChartNote], lanes_allowed: int = 5, drums: bool = False) -> list[dict]:
    """Todo lo que el editor debe avisar de un chart: [{"frame", "kind", "text"}, ...].

    Errores (los que check_playable rechaza): "error". Avisos que el juego acepta pero que no
    son lo habitual: "warn". `lanes_allowed` son los carriles de la dificultad (fácil usa 3).
    En la batería (`drums`) solo verde + amarillo es imposible, y el bit $40 es un platillo, no una cola."""
    blocked = DRUM_BLOCKED_PAIRS if drums else BLOCKED_PAIRS
    problems: list[dict] = []

    def add(frame: int, kind: str, text: str) -> None:
        problems.append({"frame": frame, "kind": kind, "text": text})

    sets = [lanes_of(n.mask) for n in chart]
    for i, (note, lanes) in enumerate(zip(chart, sets)):
        if not lanes:
            add(note.frame, "error", "nota sin carril")
        if any(lane >= lanes_allowed for lane in lanes):
            add(note.frame, "warn", f"carril fuera de los {lanes_allowed} de esta dificultad")
        if any({x, y} in blocked for x in lanes for y in lanes):
            add(note.frame, "error", "acorde imposible (izquierda + derecha de la cruz)")
        if drums:
            hands = bin(note.mask & ~KICK_MASK).count("1")
            if hands > MAX_DRUM_HANDS:
                add(note.frame, "error", f"{hands} golpes de mano a la vez: solo hay dos manos (y un pie para el bombo)")
            elif len(lanes) > MAX_DRUM_BUTTONS:
                add(note.frame, "error", f"{len(lanes)} botones a la vez: como máximo 3 (el bombo y dos golpes de mano)")
        if note.cymbal and (note.end is not None or not note.mask & TOM_MASK):
            add(note.frame, "error", "un platillo no lleva cola y solo puede ser un tom (no la tarola ni el bombo)")
        if note.end is not None:
            if note.end <= note.frame:
                add(note.frame, "error", "la cola termina antes de empezar")
                continue
            for other, other_lanes in zip(chart[i + 1 :], sets[i + 1 :]):
                if other.frame >= note.end:
                    break
                if lanes_clash(other_lanes, lanes, blocked):
                    add(other.frame, "error", f"cae mientras se sostiene la nota del cuadro {note.frame}")
        if note.star and len(lanes) > 1:
            add(note.frame, "warn", "un acorde dentro de una frase estelar")
    run = 0
    for i, note in enumerate(chart):
        run = run + 1 if note.star else 0
        if run and (i + 1 == len(chart) or not chart[i + 1].star) and run != STAR_PHRASE_LEN:
            add(chart[i - run + 1].frame, "warn", f"frase estelar de {run} notas (lo normal son {STAR_PHRASE_LEN})")
    return problems


def chart_from_json(items: list[dict]) -> list[ChartNote]:
    """Las notas que manda la página (frame, mask, end, star), revisadas y ordenadas por cuadro."""
    notes: dict[int, ChartNote] = {}
    for item in items:
        frame, mask, end = item["frame"], item["mask"], item.get("end")
        if not (isinstance(frame, int) and isinstance(mask, int) and 0 <= frame < 1 << 20 and 0 <= mask <= LANE_MASK):
            raise ValueError(f"nota inválida: {item}")
        if end is not None and not (isinstance(end, int) and end < 1 << 20):
            raise ValueError(f"cola inválida: {item}")
        if frame in notes:
            raise ValueError(f"dos notas en el cuadro {frame}")
        notes[frame] = ChartNote(frame, mask, end, bool(item.get("star", False)), bool(item.get("cymbal", False)))
    return sorted(notes.values(), key=lambda n: n.frame)


def validate_chart(key: str, chart: list[ChartNote]) -> None:
    """Falla con un mensaje claro si el chart no se puede tocar (ver chart.check_playable)."""
    try:
        check_playable(chart, DRUM_BLOCKED_PAIRS if key.startswith("bateria/") else BLOCKED_PAIRS)
        if key.startswith("bateria/"):
            for problem in chart_problems(chart, 5, True):
                if problem["kind"] == "error" and "a la vez" in problem["text"]:
                    raise ValueError(f"cuadro {problem['frame']}: {problem['text']}")
        if any(n.cymbal and (n.end is not None or not n.mask & TOM_MASK) for n in chart):
            raise ValueError("un platillo no lleva cola y solo puede ser un tom")
    except ValueError as error:
        raise ValueError(f"chart {key}: {error}") from error
