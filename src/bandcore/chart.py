"""Generación automática de charts a partir de las notas de un canal, la misma en los cuatro juegos.

Pasos:
  1. Reducir: según la dificultad, quitamos notas demasiado juntas. Se conservan
     primero las que caen en tiempos fuertes (el beat), luego medios tiempos, etc.
  2. Carriles: la nota más aguda de su vecindario va a la derecha y la más grave
     a la izquierda. Además se respeta el contorno: si la melodía sube, el carril
     sube; si repite nota, repite carril.
  3. Acordes (medio, difícil y experto): una nota se vuelve acorde de 2 carriles si
     en su gesto había un arpegio (varias alturas muy seguidas) o si el canal
     compañero (otro instrumento de la canción) toca otra altura al mismo tiempo (la armonía).

Cada instrumento tiene su papel (ROLES), pero todos siguen el contorno de su melodía y los tiempos
fuertes: un chart que no suena como la canción se siente desconectado (lo probamos con carriles fijos
por nota y notas a contratiempo para el teclado, y se jugaba peor: ver NESBand #19).
  guitarra (lead): contorno con un vecindario de 4 notas a cada lado
  teclado  (keys): contorno con un vecindario más amplio (KEYS_WINDOW): carriles distintos a los de
                   la guitarra, pero igual de suaves; sus acordes se permiten en medios tiempos
  bajo     (bass): prefiere las notas largas en los tiempos fuertes; sin acordes

Lo propio de cada juego sigue en su repo (`chart.py`): escribir los charts en su formato (chart.s, .bin, song.c) y
las reglas extra de su pantalla (GBBand separa más las gemas porque su autopista es más corta).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Note:
    """Una nota (o un golpe de batería) de un canal: lo que necesita el generador de charts."""

    frame: int  # cuadro desde que empieza la canción
    pitch: int  # número MIDI (solo importa para comparar alturas)
    duration: int
    extra: int | None = None  # otra altura del mismo gesto (un arpegio fusionado: un acorde "roto"); ver merge_attacks
    instrument: str | None = None  # qué sonido es (en los canales de batería, parte de lo que identifica un golpe)


@dataclass(frozen=True)
class ChartNote:
    frame: int  # cuadro en que suena (contando la entrada de la autopista)
    mask: int  # bits 0-4 = carriles
    end: int | None = None  # cuadro en que termina su cola (None = nota normal)
    star: bool = False  # nota de una frase de poder estelar
    cymbal: bool = False  # solo en la batería (no usa colas): el pad es un platillo, no un tom; va en el bit $40


ROLES = ("lead", "keys", "bass")
KEYS_WINDOW = 6  # vecindario de notas (a cada lado) con el que el teclado reparte sus carriles (la guitarra usa 4)


@dataclass(frozen=True)
class Difficulty:
    name: str
    lanes: int  # cuántos carriles usa (desde el verde)
    min_gap: int  # cuadros mínimos entre dos notas
    chord_gap: int = 0  # cuadros mínimos entre dos acordes (0 = sin acordes)
    chord_notes: int = 0  # notas sueltas mínimas entre dos acordes (para que no sean la mitad del chart y quepan frases estelares)


DIFFICULTIES = [
    Difficulty("facil", lanes=3, min_gap=24),
    Difficulty("medio", lanes=4, min_gap=14, chord_gap=150, chord_notes=8),
    Difficulty("dificil", lanes=5, min_gap=9, chord_gap=72, chord_notes=6),
    Difficulty("experto", lanes=5, min_gap=5, chord_gap=42, chord_notes=5),
]


@dataclass(frozen=True)
class BeatGrid:
    frames_per_beat: int
    first_beat: int  # cuadro (relativo a la música) del primer tiempo

    def strength(self, frame: int) -> int:
        """3 = en el tiempo, 2 = medio tiempo, 1 = cuarto de tiempo, 0 = otra cosa."""
        offset = (frame - self.first_beat) % self.frames_per_beat
        for level, divisions in ((3, 1), (2, 2), (1, 4)):
            step = self.frames_per_beat / divisions
            nearest = round(offset / step) * step
            if abs(offset - nearest) <= 1:
                return level
        return 0


def estimate_beat(onsets: list[int], hint: int | None = None) -> BeatGrid:
    """Estima la duración del tiempo a partir de todos los ataques de la canción.

    Un NSF o un VGM no traen tempo (son escrituras a los registros). Aquí buscamos el intervalo más común entre ataques
    (la "subdivisión") y lo duplicamos hasta caer en un tempo razonable (100-200 BPM a 60 cuadros/s).
    """
    onsets = sorted(set(onsets))
    if hint:
        period = hint
    else:
        counts: dict[int, int] = {}
        for a, b in zip(onsets, onsets[1:]):
            gap = b - a
            if 3 <= gap <= 48:
                counts[gap] = counts.get(gap, 0) + 1
        step = max(counts, key=lambda g: (counts[g], -g)) if counts else 8
        # Duplicar la subdivisión hasta llegar a 18 cuadros (200 BPM) o más
        period = step
        while period < 18:
            period *= 2
    # Fase: la que hace caer más ataques en tiempos
    best_phase = max(range(period), key=lambda p: sum(1 for f in onsets if (f - p) % period == 0))
    return BeatGrid(period, best_phase)


def merge_attacks(notes: list[Note], max_gap: int = 2) -> list[Note]:
    """Junta en una sola nota los "gestos" hechos de notas cortísimas.

    Los drivers de sonido adornan las notas cuadro por cuadro, y cada cambio de altura con la tecla presionada sale
    como una nota distinta:
      - un golpe de 1 cuadro (otro instrumento) y luego la nota sostenida
      - barridos de tono: una nota distinta en cada cuadro (bajando o subiendo)
      - arpegios rapidísimos (acordes "rotos" a 30 o 60 notas por segundo)
    Al oído cada uno es una sola nota que empieza en la primera. Regla: una nota
    de max_gap cuadros o menos se une con la que la sigue de inmediato. El grupo
    toma el tono de su nota más larga (la nota "de llegada").
    """
    groups: list[list[Note]] = []
    for note in notes:
        last = groups[-1][-1] if groups else None
        if last and last.duration <= max_gap and note.frame - last.frame <= max_gap:
            groups[-1].append(note)
        else:
            groups.append([note])
    merged = []
    for group in groups:
        first, last = group[0], group[-1]
        main = max(group, key=lambda n: n.duration)
        merged.append(Note(first.frame, main.pitch, last.frame + last.duration - first.frame, arpeggio_pitch(group, main)))
    return merged


def arpeggio_pitch(group: list[Note], main: Note) -> int | None:
    """La otra altura de un arpegio (un acorde "roto"), o None si el grupo no lo es.

    Es un arpegio si el gesto tiene 3 o más notas con al menos 2 alturas y no sube o baja siempre
    (eso es un barrido de tono). Se toma la altura más lejana a la principal, si está a entre 3 y
    12 semitonos (una tercera a una octava): cerca sería ruido y lejos no se oye como acorde."""
    if len(group) < 3:
        return None
    pitches = [n.pitch for n in group]
    if len(set(pitches)) < 2 or pitches == sorted(pitches) or pitches == sorted(pitches, reverse=True):
        return None
    far = max(set(pitches), key=lambda p: (abs(p - main.pitch), p))
    return far if 3 <= abs(far - main.pitch) <= 12 else None


def reduce_notes(notes: list[Note], difficulty: Difficulty, grid: BeatGrid, role: str = "lead") -> list[Note]:
    """Quita notas hasta que ninguna quede a menos de min_gap cuadros de otra.

    Se conservan primero las de más prioridad: los tiempos fuertes; el bajo, además, prefiere las notas
    largas. (Probamos que el teclado prefiriera lo que cae entre los tiempos y se sentía desconectado
    del ritmo de la canción: ver NESBand #19.)"""
    if role == "bass":
        by_priority = sorted(notes, key=lambda n: (-grid.strength(n.frame), -n.duration, n.frame))
    else:
        by_priority = sorted(notes, key=lambda n: (-grid.strength(n.frame), n.frame))
    kept: list[Note] = []
    for note in by_priority:
        if all(abs(note.frame - other.frame) >= difficulty.min_gap for other in kept):
            kept.append(note)
    return sorted(kept, key=lambda n: n.frame)


def assign_lanes(notes: list[Note], lanes: int, window: int = 4) -> list[int]:
    """Un carril (0..lanes-1) por nota, siguiendo la altura y el contorno."""
    result: list[int] = []
    for i, note in enumerate(notes):
        neighborhood = sorted({n.pitch for n in notes[max(0, i - window) : i + window + 1]})
        if len(neighborhood) == 1:
            lane = result[-1] if result else lanes // 2
        else:
            rank = neighborhood.index(note.pitch)
            lane = round(rank * (lanes - 1) / (len(neighborhood) - 1))

        if result:
            prev_lane, prev_pitch = result[-1], notes[i - 1].pitch
            if note.pitch == prev_pitch:
                lane = prev_lane
            elif note.pitch > prev_pitch and lane <= prev_lane:
                lane = min(prev_lane + 1, lanes - 1)
            elif note.pitch < prev_pitch and lane >= prev_lane:
                lane = max(prev_lane - 1, 0)
        result.append(lane)
    return result


STAR_FLAG = 0x20  # en la entrada de una nota: forma parte de una frase de poder estelar
STAR_PHRASE_LEN = 4  # notas por frase (si aciertas todas, la barra sube 25 %)
STAR_FIRST = 10  # índice de la primera nota de la primera frase
STAR_SPACING = 24  # notas entre el inicio de una frase y el de la siguiente
SUSTAIN_FLAG = 0x40  # en la entrada de una nota: tiene cola (su fin va en otra entrada); en la batería, platillo
END_FLAG = 0x80  # entrada de fin de cola: los bits 0-4 son los carriles que la sueltan
MIN_SUSTAIN = 16  # cuadros mínimos de cola (0.27 s); más corta no vale la pena mantenerla
SUSTAIN_TRIM = 4  # la cola termina un poco antes de que acabe la nota: soltar a tiempo no castiga
SUSTAIN_GAP = 3  # cuadros libres entre el fin de una cola y la siguiente nota del mismo carril


# Los carriles verde, rojo y amarillo son izquierda, abajo y derecha de la cruz del control, que no deja apretar
# izquierda y derecha a la vez: verde + amarillo (carriles 0 y 2) es imposible. Con la opción ESPEJO los carriles se
# invierten (0 con 4, 1 con 3) y amarillo + naranja (2 y 4) pasaría a ser verde + amarillo. Por eso ningún chart pide
# esas dos parejas a la vez, ni en un acorde ni con una nota mientras se sostiene la otra.
BLOCKED_PAIRS = ({0, 2}, {2, 4})

# En la batería el bombo es el carril naranja pero se pisa con el pie, no con la cruz: ahí solo verde + amarillo (los
# dos toms de los extremos) es imposible. (ESPEJO no aplica a la batería.)
DRUM_BLOCKED_PAIRS = ({0, 2},)


def lanes_clash(a: set[int], b: set[int], blocked=BLOCKED_PAIRS) -> bool:
    """¿Dos notas con estos carriles no pueden estar activas al mismo tiempo? (mismo carril, o una pareja bloqueada)"""
    return bool(a & b) or any({x, y} in blocked for x in a for y in b)


def sustain_ends(notes: list[Note], lanes: list[int | tuple[int, ...]], gap: int = SUSTAIN_GAP) -> list[int | None]:
    """Fin de la cola de cada nota, o None si dura muy poco.

    La cola dura lo que la nota, pero termina antes de la siguiente nota que comparta un
    carril con ella (con `gap` cuadros libres) para poder soltar y volver a tocar.
    Cada elemento de `lanes` es un carril o, en un acorde, la tupla de sus carriles.
    """
    sets = [set(lane) if isinstance(lane, tuple) else {lane} for lane in lanes]
    ends: list[int | None] = []
    for i, note in enumerate(notes):
        limit = note.frame + note.duration
        for other, other_lanes in zip(notes[i + 1 :], sets[i + 1 :]):
            if lanes_clash(other_lanes, sets[i]):
                limit = min(limit, other.frame - gap)
                break
        length = limit - note.frame - SUSTAIN_TRIM
        ends.append(note.frame + length if length >= MIN_SUSTAIN else None)
    return ends


def second_lane(lane: int, lanes: int, delta: int) -> int | None:
    """Carril del segundo botón de un acorde, a 1 carril (terceras) o 2 (más lejos) del primero y del
    lado de la otra altura; si no cabe de ese lado, del otro, o con la otra distancia. Nunca una pareja
    bloqueada (BLOCKED_PAIRS). None si no cabe en ninguno."""
    step = 1 if abs(delta) < 5 else 2
    sign = 1 if delta > 0 else -1
    for distance in (step, 3 - step):
        for direction in (sign, -sign):
            candidate = lane + direction * distance
            if 0 <= candidate < lanes and {lane, candidate} not in BLOCKED_PAIRS:
                return candidate
    return None


def chord_masks(
    notes: list[Note], lanes: list[int], difficulty: Difficulty, grid: BeatGrid, role: str, partner: list[Note]
) -> list[int]:
    """La máscara de carriles de cada nota: un bit, o dos si la nota se vuelve acorde.

    Una nota es candidata si el chart lo permite (medio en adelante, sin bajo), cae en un tiempo
    fuerte (la guitarra pide medio tiempo o más; el teclado, cualquier subdivisión), hay aire antes
    de la siguiente nota (no se pide un acorde en medio de una carrera) y pasó chord_gap cuadros
    desde el último acorde. Su segunda altura sale de su arpegio fusionado o, si no hay, de la nota
    que el canal compañero toca al mismo tiempo (±2 cuadros) a 3 o más semitonos."""
    masks = [1 << lane for lane in lanes]
    if not difficulty.chord_gap or role == "bass":
        return masks
    min_strength = 2 if role == "lead" else 1
    by_frame = {n.frame: n for n in partner}
    last_chord = -(10**9)
    last_chord_index = -(10**9)
    for i, (note, lane) in enumerate(zip(notes, lanes)):
        next_gap = notes[i + 1].frame - note.frame if i + 1 < len(notes) else 10**9
        if (
            i < 2
            or note.frame - last_chord < difficulty.chord_gap
            or i - last_chord_index <= difficulty.chord_notes
            or next_gap < difficulty.min_gap * 2
            or grid.strength(note.frame) < min_strength
        ):
            continue
        other = note.extra
        if other is None:
            for d in range(-2, 3):
                near = by_frame.get(note.frame + d)
                if near and abs(near.pitch - note.pitch) >= 3:
                    other = near.pitch
                    break
        if other is None:
            continue
        lane2 = second_lane(lane, difficulty.lanes, other - note.pitch)
        if lane2 is not None and lane2 != lane:
            masks[i] |= 1 << lane2
            last_chord = note.frame
            last_chord_index = i
    return masks


def build_chart(
    notes: list[Note],
    difficulty: Difficulty,
    grid: BeatGrid,
    lead_in: int,
    role: str = "lead",
    partner: list[Note] | None = None,
) -> list[ChartNote]:
    """El chart guarda cuadros, no filas: así sirve para cualquier velocidad de
    la autopista (el juego calcula en qué fila se dibuja cada nota).

    role es el papel del instrumento (ver ROLES) y partner las notas del canal compañero,
    de donde sale la armonía de los acordes."""
    reduced = reduce_notes(merge_attacks(notes), difficulty, grid, role)
    # El contorno de la melodía manda en todos; el teclado mira un vecindario más amplio (KEYS_WINDOW notas a cada
    # lado y no 4), así sus carriles siguen la melodía pero no son los mismos que los de la guitarra
    lanes = assign_lanes(reduced, difficulty.lanes, window=KEYS_WINDOW if role == "keys" else 4)
    masks = chord_masks(reduced, lanes, difficulty, grid, role, merge_attacks(partner or []))
    ends = sustain_ends(reduced, [tuple(b for b in range(5) if mask >> b & 1) for mask in masks])
    stars = star_notes(len(reduced), [mask & (mask - 1) != 0 for mask in masks])
    return [
        ChartNote(lead_in + note.frame, mask, None if end is None else lead_in + end, star)
        for note, mask, end, star in zip(reduced, masks, ends, stars)
    ]


def star_notes(count: int, chords: list[bool] | None = None) -> list[bool]:
    """Qué notas forman frases de poder estelar: grupos de STAR_PHRASE_LEN notas
    seguidas, separados por STAR_SPACING notas. El juego reconoce el final de una
    frase porque la nota que sigue ya no es estelar.

    Un acorde no entra en una frase (el juego cuenta cada botón de un acorde como una nota y
    premiaría la frase dos veces): si cae en una, la frase se recorre hasta que haya
    STAR_PHRASE_LEN notas sueltas seguidas."""
    chords = chords or [False] * count
    stars = [False] * count
    for start in range(STAR_FIRST, count - STAR_PHRASE_LEN, STAR_SPACING):
        for begin in range(start, min(start + STAR_SPACING - STAR_PHRASE_LEN - 1, count - STAR_PHRASE_LEN)):
            if not any(chords[begin : begin + STAR_PHRASE_LEN]):
                for i in range(begin, begin + STAR_PHRASE_LEN):
                    stars[i] = True
                break
    return stars


CHART_END = 0xFF  # carriles = $FF marca el fin del chart
MAX_DELTA = 255


def chart_entries(chart: list[ChartNote]) -> list[tuple[int, int]]:
    """El chart como lo lee el juego: pares (cuadros desde la entrada anterior, carriles).

    Si dos entradas están a más de 255 cuadros, se intercalan entradas de relleno
    sin carriles (carriles = 0) que solo hacen avanzar el tiempo.

    Una nota con cola lleva SUSTAIN_FLAG, y su fin es otra entrada con END_FLAG y los
    carriles que se sueltan en ese cuadro (así el formato sigue siendo de 2 bytes).
    NGBand usa su propia versión (parte también los saltos de 255 exactos).
    """
    events: list[tuple[int, int]] = []  # (cuadro, carriles)
    end_masks: dict[int, int] = {}
    for note in chart:
        mask = note.mask | (STAR_FLAG if note.star else 0)
        if note.cymbal:
            if note.end is not None:
                raise ValueError(f"nota del cuadro {note.frame}: un platillo no lleva cola (comparten el bit $40)")
            mask |= SUSTAIN_FLAG
        if note.end is None:
            events.append((note.frame, mask))
        else:
            events.append((note.frame, mask | SUSTAIN_FLAG))
            end_masks[note.end] = end_masks.get(note.end, 0) | note.mask
    events += [(frame, END_FLAG | mask) for frame, mask in end_masks.items()]
    events.sort(key=lambda e: e[0])  # estable: a igual cuadro, las notas van antes que los fines

    entries: list[tuple[int, int]] = []
    previous = 0
    for frame, mask in events:
        delta = frame - previous
        while delta > MAX_DELTA:
            entries.append((MAX_DELTA, 0))
            delta -= MAX_DELTA
        entries.append((delta, mask))
        previous = frame
    entries.append((0, CHART_END))
    return entries


def chart_size(chart: list[ChartNote]) -> int:
    return 2 * len(chart_entries(chart))


def check_playable(chart: list[ChartNote], blocked=BLOCKED_PAIRS) -> None:
    """Falla si el chart pide algo imposible con la cruz del control: dos carriles de una pareja bloqueada
    (BLOCKED_PAIRS; en la batería DRUM_BLOCKED_PAIRS) a la vez, ya sea en un acorde o tocando una nota mientras se
    sostiene otra."""
    sets = [{b for b in range(5) if n.mask >> b & 1} for n in chart]
    for i, (note, lanes) in enumerate(zip(chart, sets)):
        if any({x, y} in blocked for x in lanes for y in lanes):
            raise ValueError(f"acorde imposible en el cuadro {note.frame}: carriles {sorted(lanes)}")
        if note.end is None:
            continue
        for other, other_lanes in zip(chart[i + 1 :], sets[i + 1 :]):
            if other.frame >= note.end:
                break
            if lanes_clash(other_lanes, lanes, blocked):
                raise ValueError(f"la nota del cuadro {other.frame} cae mientras se sostiene la del {note.frame}")
