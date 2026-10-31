"""Batería: de los golpes ya asignados a un papel, a un chart de 4 pads y bombo al estilo de Rock Band.

Lo mismo en los cuatro juegos. Una canción no dice qué es cada golpe, así que cada juego agrupa los golpes de sus
canales de batería (ruido y DPCM en el NES, ruido y DAC en la Genesis, ADPCM en la Neo Geo...) y el usuario, o una
sugerencia automática, le asigna a cada grupo un papel. Esa asignación vive en src/songs/<canción>/drums.json, aparte
del chart, para que `reimport` no la pierda. Agrupar y sugerir es propio de cada chip y sigue en cada juego; aquí van
los papeles, drums.json y el chart (build_drum_chart).

Papeles y carriles (cada carril es el color de siempre, que fija el botón del control):
    tarola = rojo   tom 1 / hi-hat = amarillo   tom 2 / ride = azul   tom 3 / crash = verde   bombo = naranja, la barra
En experto (el "Pro" de Rock Band) un golpe de platillo se marca con el bit $40 (la batería no usa colas); va en el
mismo carril y botón que un tom, solo se ve distinto. En las demás dificultades los platillos se tocan como toms.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .chart import DIFFICULTIES, DRUM_BLOCKED_PAIRS, BeatGrid, ChartNote, star_notes

DRUMS_FILE = "drums.json"
DRUMS_VERSION = 1
ROLE_LANE = {"tom3": 0, "snare": 1, "tom1": 2, "tom2": 3, "kick": 4}
ROLE_LABELS = {"kick": "Bombo (barra naranja)", "snare": "Tarola (rojo)", "tom1": "Tom 1 / Hi-hat (amarillo)",
               "tom2": "Tom 2 / Ride (azul)", "tom3": "Tom 3 / Crash (verde)"}
ROLES = tuple(ROLE_LABELS)
ROLE_WEIGHT = {"kick": 3, "snare": 3, "tom1": 2, "tom2": 2, "tom3": 2}
# Con menos dificultad, los toms se juntan en los que quedan: fácil usa bombo, tarola y tom 1; medio agrega el 2
FOLD = {"facil": {"tom2": "tom1", "tom3": "tom1"}, "medio": {"tom3": "tom2"}, "dificil": {}, "experto": {}}
# Botones a la vez: como máximo 3 (el pie y dos manos); fácil y medio quedan en 1 y 2
MAX_BUTTONS = {"facil": 1, "medio": 2, "dificil": 3, "experto": 3}
MAX_HANDS = 2   # golpes de mano a la vez (sin contar el bombo): solo hay dos manos
PRO_CYMBALS = {"experto"}   # dificultades donde los platillos se distinguen de los toms
CYMBAL_WEIGHT_PENALTY = 0.5   # los platillos (hi-hat) son lo primero que se quita al reducir
MERGE_FRAMES = 1   # golpes a esta distancia son el mismo momento
MIN_GROUP_HITS = 3   # sugerencia: grupos con menos golpes no se asignan


def load_drums(song_dir: Path) -> dict[str, dict] | None:
    """La asignación guardada (grupo -> {"lane", "cymbal"}), o None si la canción aún no tiene."""
    path = song_dir / DRUMS_FILE
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != DRUMS_VERSION:
        raise ValueError(f"{path}: versión de drums.json no soportada ({data.get('version')})")
    return data.get("groups", {})


def check_assignment(assignment: dict[str, dict]) -> None:
    for key, item in assignment.items():
        if item.get("lane") not in (*ROLES, None):
            raise ValueError(f"{key}: papel desconocido {item.get('lane')!r}")


def dump_drums(assignment: dict[str, dict]) -> str:
    lines = [f'{{\n  "version": {DRUMS_VERSION},\n  "groups": {{']
    lines.append(",\n".join(
        f"    {json.dumps(key, ensure_ascii=False)}: {json.dumps({'lane': item.get('lane'), 'cymbal': bool(item.get('cymbal'))})}"
        for key, item in sorted(assignment.items())))
    lines.append("  }\n}\n")
    return "\n".join(lines)


def save_drums(song_dir: Path, assignment: dict[str, dict]) -> None:
    check_assignment(assignment)
    (song_dir / DRUMS_FILE).write_text(dump_drums(assignment), encoding="utf-8", newline="\n")


# --- Chart --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    frame: int
    role: str
    cymbal: bool


def build_drum_chart(hits: list[Hit], difficulty_name: str, grid: BeatGrid, lead_in: int) -> list[ChartNote]:
    """El chart de batería de una dificultad.

    1. Los toms se juntan según la dificultad (FOLD) y los golpes casi simultáneos son un "momento".
    2. De cada momento quedan hasta MAX_BUTTONS botones (primero bombo y tarola), con a lo más MAX_HANDS
       golpes de mano y sin verde + amarillo (DRUM_BLOCKED_PAIRS).
    3. Se quitan momentos hasta que ninguno quede a menos de min_gap cuadros de otro, conservando los
       de los tiempos fuertes y los de bombo y tarola.
    4. Los platillos solo se marcan en las dificultades Pro (PRO_CYMBALS).
    (GBBand usa su propia versión: además separa las gemas del mismo carril para su autopista corta.)"""
    difficulty = next(d for d in DIFFICULTIES if d.name == difficulty_name)
    fold, max_buttons = FOLD[difficulty_name], MAX_BUTTONS[difficulty_name]
    moments: list[dict] = []   # {"frame", "lanes": {lane: (peso, platillo)}}
    for hit in hits:
        role = fold.get(hit.role, hit.role)
        weight = ROLE_WEIGHT[role] - (CYMBAL_WEIGHT_PENALTY if hit.cymbal else 0)
        if moments and hit.frame - moments[-1]["frame"] <= MERGE_FRAMES:
            lanes = moments[-1]["lanes"]
            old = lanes.get(ROLE_LANE[role])
            lanes[ROLE_LANE[role]] = (max(weight, old[0]) if old else weight, hit.cymbal and (old[1] if old else True))
        else:
            moments.append({"frame": hit.frame, "lanes": {ROLE_LANE[role]: (weight, hit.cymbal)}})
    pro = difficulty_name in PRO_CYMBALS
    for moment in moments:
        lanes = moment["lanes"]
        if not pro and any(not cymbal for _, cymbal in lanes.values()):
            # Sin Pro un platillo es un tom más: si el momento ya tiene un golpe de bombo o tarola, el
            # platillo se omite (si no, casi todo sería un acorde con el hi-hat)
            lanes = {lane: value for lane, value in lanes.items() if not value[1]}
        ranked = sorted(lanes.items(), key=lambda item: -item[1][0])
        kept: dict[int, tuple] = {}
        for lane, value in ranked:
            is_hand = lane != ROLE_LANE["kick"]
            hands = sum(1 for other in kept if other != ROLE_LANE["kick"])
            if (len(kept) < max_buttons and (not is_hand or hands < MAX_HANDS)
                    and not any({lane, other} in DRUM_BLOCKED_PAIRS for other in kept)):
                kept[lane] = value
        moment["lanes"] = kept
        moment["weight"] = max((w for w, _ in kept.values()), default=0)
    moments = [m for m in moments if m["lanes"]]
    by_priority = sorted(moments, key=lambda m: (-grid.strength(m["frame"]), -m["weight"], m["frame"]))
    chosen: list[dict] = []
    for moment in by_priority:
        if all(abs(moment["frame"] - other["frame"]) >= difficulty.min_gap for other in chosen):
            chosen.append(moment)
    chosen.sort(key=lambda m: m["frame"])
    masks = [sum(1 << lane for lane in m["lanes"]) for m in chosen]
    stars = star_notes(len(chosen), [mask & (mask - 1) != 0 for mask in masks])
    return [
        ChartNote(lead_in + m["frame"], mask, None, star,
                  pro and any(cymbal for lane, (_, cymbal) in m["lanes"].items() if lane not in (ROLE_LANE["kick"], ROLE_LANE["snare"])))
        for m, mask, star in zip(chosen, masks, stars)
    ]
