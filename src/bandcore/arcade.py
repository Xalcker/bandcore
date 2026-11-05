"""Las notas de una canción de arcade con YM2151 (CPS1, System 16, OutRun, Konami, X68000) en el formato común
(`bandcore.notes`), para arreglarla en NESBand, GBBand, GENBand o NGBand.

Ningún juego toca el YM2151, así que esto vive en bandcore (`band export-notes` y `band channels`):

  1. Las notas de los 8 canales FM y el ruido (`bandcore.ym2151`) y los golpes del chip de samples (`bandcore.pcm`).
  2. Qué canal es cada instrumento (`auto_assign`): primero se apartan los canales FM de batería (casi siempre la misma
     altura, o golpes hechos de barridos de tono cuadro por cuadro); el bajo es el más grave y la guitarra el que más
     notas tiene contando más las agudas. Los arcades duplican canales (el mismo papel en dos canales, con otro
     detune): un canal casi igual a uno ya elegido no se elige otra vez.
  3. La batería: los golpes se agrupan (un sample del chip PCM, o un canal FM con su patch y su altura) y cada grupo
     recibe un papel (`suggest_assignment`), que se puede corregir en un drums.json como el de los juegos.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path

from . import notes as bnotes
from . import pcm, ym2151
from .chart import Note, merge_attacks
from .drums import MIN_GROUP_HITS, ROLES, check_assignment
from .vgm import SAMPLE_RATE, SAMPLES_PER_FRAME, Vgm, read_vgm

DEFAULT_SECONDS = 120  # con bucle, esto (una canción corta repite el bucle, una larga se corta)
MAX_FRAMES = 64000 - 60 * 8  # los juegos cuentan los cuadros en 16 bits (y les falta la entrada de la autopista)
INSTRUMENTS = ("guitarra", "teclado", "bajo")
SLOTS = {"guitarra": "Square1", "teclado": "Square2", "bajo": "Triangle"}
MIN_NOTES = 20  # un canal con menos notas no es un instrumento
MIN_PITCHES = 5  # un canal que casi siempre toca la misma altura es percusión (un bombo FM)
SWEEP_RATIO = 1.4  # notas por golpe: los golpes FM de batería son barridos de tono, muchas notas cortísimas
BASS_MAX_MEDIAN = 56  # el bajo tiene la mediana por debajo de La bemol 3
BASS_MIN_PITCHES = 3
DUPLICATE_SHARE = 0.8  # un canal que repite la misma nota (u octava) en esta parte de las notas de otro es su doble
DUPLICATE_DELAY = 12  # cuadros de retraso de un eco
HIHAT_CROSSINGS = 5000  # cruces por segundo: arriba de esto un sample es un platillo
SNARE_CROSSINGS = 1500  # entre esto y el platillo, una tarola
CRASH_SECONDS = 0.4  # un platillo que dura más que esto es un crash (o un ride), no un hi-hat
HIHAT_PITCH = 75  # en los canales FM, un golpe más agudo que esto es un hi-hat
KICK_PITCH = 50  # y uno más grave, un bombo
DAC_MIN_SECONDS = 0.03  # samples más cortos son restos de un golpe cortado
DAC_COMMON = 6  # bombo y tarola salen de los samples que suenan al menos 1/6 de lo que suena el que más

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def midi_name(pitch: int) -> str:
    return f"{NAMES[pitch % 12]}{pitch // 12 - 1}"


# --- Instrumentos -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stats:
    count: int  # notas tras juntar los adornos (merge_attacks)
    median: float
    pitches: int  # alturas distintas
    sweep: float  # notas por golpe (notas sin juntar / juntadas)
    volume: float


def channel_stats(ns: list[Note]) -> Stats | None:
    if not ns:
        return None
    merged = merge_attacks(ns)
    return Stats(len(merged), statistics.median(n.pitch for n in merged), len({n.pitch for n in merged}),
                 len(ns) / len(merged), statistics.median(getattr(n, "volume", 15) for n in ns))


def is_fm_drum(stats: Stats | None) -> bool:
    """Un canal FM de batería: golpes hechos de barridos de tono, o siempre la misma altura. (Un canal con pocas
    alturas pero sin barridos suele ser un acorde repartido en varios canales: no es batería ni instrumento.)"""
    return stats is not None and stats.count >= MIN_NOTES and (stats.sweep >= SWEEP_RATIO or stats.pitches == 1)


def duplicate_of(a: list[Note], b: list[Note]) -> bool:
    """`a` es el doble de `b`: casi todas sus notas empiezan con la misma nota (o a una octava) que una de `b`, a la
    misma distancia (hasta DUPLICATE_DELAY cuadros antes o después, con un cuadro de tolerancia): un eco o un coro."""
    if not a or not b:
        return False
    starts = {n.frame: n.pitch for n in b}

    def matches(n: Note, frame: int) -> bool:
        return any((p := starts.get(frame + d)) is not None and (n.pitch - p) % 12 == 0 for d in (-1, 0, 1))

    for delay in range(-DUPLICATE_DELAY, DUPLICATE_DELAY + 1):
        if sum(1 for n in a if matches(n, n.frame - delay)) >= DUPLICATE_SHARE * len(a):
            return True
    return False


def auto_assign(notes: dict[str, list[Note]]) -> dict[str, list[str]]:
    """Un canal FM por instrumento (los que se puedan): ver el docstring del módulo. El bajo puede tener pocas alturas
    (un bajo pedal); la guitarra y el teclado, al menos MIN_PITCHES."""
    stats = {ch: s for ch in ym2151.CHANNEL_NAMES[:ym2151.CHANNELS]
             if (s := channel_stats(notes.get(ch, []))) and s.count >= MIN_NOTES and s.pitches >= BASS_MIN_PITCHES
             and not is_fm_drum(s)}
    out: dict[str, list[str]] = {}

    def take(name: str, ch: str) -> None:
        out[name] = [ch]
        for other in list(stats):
            if other == ch or duplicate_of(notes[other], notes[ch]):
                stats.pop(other)

    low = [ch for ch, s in stats.items() if s.median < BASS_MAX_MEDIAN]
    if low:
        take("bajo", min(low, key=lambda ch: stats[ch].median))
    for name in ("guitarra", "teclado"):
        # Sin melodía (una canción de bajo y acordes), un canal del acorde: mejor eso que un instrumento vacío
        melodic = [c for c, s in stats.items() if s.pitches >= MIN_PITCHES] or list(stats)
        if melodic:
            take(name, max(melodic, key=lambda c: stats[c].count * max(0.25, 1 + (stats[c].median - 60) / 24)))
    return out


def drum_channels(notes: dict[str, list[Note]], melodic: set[str]) -> list[str]:
    """El ruido, el chip de samples y los canales FM de batería que no son de otro instrumento."""
    out = [ch for ch in (ym2151.NOISE, *pcm.CHANNEL.values()) if notes.get(ch)]
    out += [ch for ch in ym2151.CHANNEL_NAMES[:ym2151.CHANNELS]
            if ch not in melodic and is_fm_drum(channel_stats(notes.get(ch, [])))]
    return out


# --- Batería ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Group:
    key: str  # "oki:11" (un sample), "ruido:20" (el ruido, por frecuencia) o "fm4:C2" (un canal FM, por altura)
    channel: str
    count: int
    pitch: int  # en los canales FM, la altura del golpe; en el ruido, su frecuencia
    sample: pcm.Sample | None = None

    @property
    def description(self) -> str:
        if self.sample:
            return f"sample de {self.sample.seconds:.2f} s, {self.sample.crossings:.0f} cruces/s"
        return f"frecuencia {self.pitch}" if self.channel == ym2151.NOISE else midi_name(self.pitch)


def strikes(channel: str, ns: list[Note]) -> list[Note]:
    """Los golpes de un canal de batería. En los canales FM, un golpe hecho de notas cortísimas (un barrido de tono)
    cuenta una vez, con la altura y el patch de su ataque."""
    if channel in pcm.CHANNEL.values() or channel == ym2151.NOISE:
        return sorted(ns, key=lambda n: n.frame)
    out: list[Note] = []
    previous: Note | None = None
    for n in sorted(ns, key=lambda n: n.frame):
        # Como merge_attacks: una nota de 2 cuadros o menos seguida de inmediato por otra es parte del mismo golpe
        if not (previous and previous.duration <= 2 and n.frame - previous.frame <= 2):
            out.append(n)
        previous = n
    return out


def group_key(channel: str, note: Note) -> str:
    if channel in pcm.CHANNEL.values():
        return note.instrument or f"{channel}:{note.pitch}"
    if channel == ym2151.NOISE:
        return f"{channel}:{note.pitch}"
    return f"{channel}:{midi_name(note.pitch)}"  # el patch no: los drivers lo retocan en cada golpe


def drum_groups(notes: dict[str, list[Note]], channels: list[str], samples: dict[str, pcm.Sample]) -> list[Group]:
    found: dict[str, list] = {}
    for ch in channels:
        for n in strikes(ch, notes.get(ch, [])):
            found.setdefault(group_key(ch, n), [ch, 0, n.pitch])[1] += 1
    return sorted((Group(key, ch, count, pitch, samples.get(key)) for key, (ch, count, pitch) in found.items()),
                  key=lambda g: (channels.index(g.channel), -g.count))


def suggest_assignment(groups: list[Group]) -> dict[str, dict]:
    """Una primera asignación (grupo -> {"lane", "cymbal"}), el punto de partida para corregir en drums.json.

    Samples: el más grave de los comunes es el bombo; los que cruzan el cero más de HIHAT_CROSSINGS veces por segundo
    son platillos (hi-hat si son cortos, crash o ride si no); la tarola es el más común de en medio; los demás, toms.
    Canales FM (cuando no hay chip de samples): los graves son bombo, los agudos hi-hat y el más común de en medio la
    tarola. El ruido es hi-hat si es agudo y tarola si no."""
    assignment: dict[str, dict] = {g.key: {"lane": None, "cymbal": False} for g in groups}
    used: set[str] = set()

    def give(group: Group, preferred: list[str], cymbal: bool = False) -> None:
        role = next((r for r in preferred if r not in used), preferred[-1])
        assignment[group.key] = {"lane": role, "cymbal": cymbal}
        used.add(role)

    sampled = [g for g in groups if g.sample and g.count >= MIN_GROUP_HITS and g.sample.seconds >= DAC_MIN_SECONDS]
    if sampled:
        cymbals = [g for g in sampled if g.sample.crossings >= HIHAT_CROSSINGS]
        others = [g for g in sampled if g not in cymbals]
        top = max((g.count for g in others), default=0)  # sin los platillos: el hi-hat suena más que todo
        drums = [g for g in others if g.count * DAC_COMMON >= top]
        if drums:
            give(min(drums, key=lambda g: g.sample.crossings), ["kick"])
        snares = [g for g in drums if SNARE_CROSSINGS <= g.sample.crossings and assignment[g.key]["lane"] is None]
        snares = snares or [g for g in drums if assignment[g.key]["lane"] is None]
        if snares:
            give(max(snares, key=lambda g: g.count), ["snare"])
        for g in sorted(cymbals, key=lambda g: -g.count):
            if g.sample.seconds > CRASH_SECONDS:
                give(g, ["tom3", "tom2"], cymbal=True)
            else:
                give(g, ["tom1", "tom2"], cymbal=True)
        rest = sorted((g for g in sampled if assignment[g.key]["lane"] is None), key=lambda g: g.sample.crossings)
        for g in rest:
            give(g, ["tom3", "tom2", "tom1"])
        return assignment

    tonal = [g for g in groups if g.channel != ym2151.NOISE and g.count >= MIN_GROUP_HITS]
    if tonal:
        top = max(g.count for g in tonal)
        common = [g for g in tonal if g.count * DAC_COMMON >= top]
        lows = [g for g in common if g.pitch < KICK_PITCH] or [min(common, key=lambda g: g.pitch)]
        give(max(lows, key=lambda g: g.count), ["kick"])
        for g in tonal:
            if assignment[g.key]["lane"] is None and g.pitch >= HIHAT_PITCH:
                assignment[g.key] = {"lane": "tom1", "cymbal": True}
                used.add("tom1")
        middle = [g for g in common if assignment[g.key]["lane"] is None]
        if middle:
            give(max(middle, key=lambda g: g.count), ["snare"])
    for g in sorted((g for g in groups if g.channel == ym2151.NOISE and g.count >= MIN_GROUP_HITS), key=lambda g: -g.pitch):
        if g.pitch >= 28:
            assignment[g.key] = {"lane": "tom1", "cymbal": True}
        else:
            give(g, ["snare", "tom2", "tom3"])
    for g in sorted((g for g in tonal if assignment[g.key]["lane"] is None), key=lambda g: g.pitch):
        give(g, ["tom3", "tom2", "tom1"])
    return assignment


def drum_hits(notes: dict[str, list[Note]], channels: list[str], assignment: dict[str, dict]) -> list[list]:
    """Los golpes como los pide bandcore.notes: [cuadro, papel, platillo]."""
    hits = []
    for ch in channels:
        for n in strikes(ch, notes.get(ch, [])):
            item = assignment.get(group_key(ch, n))
            if item and item.get("lane"):
                hits.append([n.frame, item["lane"], bool(item.get("cymbal")) and item["lane"] not in ("kick", "snare")])
    return sorted(hits)


# --- La canción ---------------------------------------------------------------------------------------------


@dataclass
class Song:
    vgm: Vgm
    title: str
    game: str
    frames: int
    notes: dict[str, list[Note]]  # fm1-fm8, ruido y el canal del chip de samples
    samples: dict[str, pcm.Sample]
    assign: dict[str, list[str]]  # instrumento -> canales
    drums: list[str]  # canales de batería
    groups: list[Group] = field(default_factory=list)
    assignment: dict[str, dict] = field(default_factory=dict)

    def channel_notes(self, channels: list[str]) -> list[Note]:
        return sorted((n for ch in channels for n in self.notes.get(ch, [])), key=lambda n: n.frame)


def song_samples(vgm: Vgm, seconds: float | None) -> int:
    """Cuántas muestras se toman: las pedidas o, sin pedir, el archivo entero; con bucle, DEFAULT_SECONDS (como
    import-song y export-notes de GENBand y NGBand: repite el bucle o corta una canción larga)."""
    if seconds is not None:
        total = round(seconds * SAMPLE_RATE)
    elif vgm.loop_sample is not None and vgm.loop_write is not None:
        total = DEFAULT_SECONDS * SAMPLE_RATE
    else:
        total = vgm.end_sample
    return min(total, MAX_FRAMES * SAMPLES_PER_FRAME)


def channel_list(text: str | None, notes: dict[str, list[Note]]) -> list[str] | None:
    if not text:
        return None
    out = [c.strip().lower() for c in text.split(",") if c.strip()]
    valid = list(ym2151.CHANNEL_NAMES) + list(pcm.CHANNEL.values())
    for c in out:
        if c not in valid:
            raise ValueError(f"canal desconocido: {c} (los canales son {', '.join(valid)})")
    return out


def load(path: Path | str, seconds: float | None = None, title: str | None = None, game: str | None = None,
         channels: dict[str, str | None] | None = None, assignment: dict[str, dict] | None = None) -> Song:
    """Lee la canción. `channels` puede fijar los canales de "guitarra", "teclado", "bajo" y "bateria" (texto con
    comas, como en la línea de comandos); `assignment`, los papeles de la batería (drums.json)."""
    vgm = read_vgm(path)
    if "ym2151" not in vgm.used:
        raise ValueError(f"{path}: no tiene YM2151")
    total = song_samples(vgm, seconds)
    notes: dict[str, list[Note]] = dict(ym2151.extract_notes(vgm, total))
    hits, samples = pcm.extract_hits(vgm, total)
    notes.update(hits)
    channels = channels or {}
    assign = auto_assign(notes)
    for name in INSTRUMENTS:
        chosen = channel_list(channels.get(name), notes)
        if chosen is not None:
            assign[name] = chosen
    melodic = {c for chs in assign.values() for c in chs}
    drums = channel_list(channels.get("bateria"), notes) or drum_channels(notes, melodic)
    song = Song(vgm, title or vgm.title or Path(path).stem, game or vgm.game, total // SAMPLES_PER_FRAME, notes, samples,
                assign, drums)
    song.groups = drum_groups(notes, drums, samples)
    song.assignment = suggest_assignment(song.groups)
    if assignment is not None:
        check_assignment(assignment)
        song.assignment = {g.key: assignment.get(g.key, {"lane": None, "cymbal": False}) for g in song.groups}
    return song


def to_notes(song: Song) -> dict:
    """El diccionario de bandcore.notes."""
    channels = {
        SLOTS[name]: [[n.frame, n.duration, n.pitch, getattr(n, "volume", bnotes.DEFAULT_VOLUME)]
                      for n in song.channel_notes(song.assign.get(name, []))]
        for name in INSTRUMENTS
    }
    return bnotes.make(song.title, song.game, song.frames, channels, drum_hits(song.notes, song.drums, song.assignment))


def describe(song: Song) -> list[str]:
    """Los canales (para elegir a mano) y los grupos de la batería con su papel."""
    lines = [f"{song.title} ({song.game}), {song.frames / 60:.1f} s"]
    role_of = {c: name for name, chs in song.assign.items() for c in chs}
    for ch, ns in song.notes.items():
        if not ns:
            continue
        s = channel_stats(ns)
        tag = role_of.get(ch) or ("batería" if ch in song.drums else "")
        if ch in pcm.CHANNEL.values() or ch == ym2151.NOISE:
            lines.append(f"  {ch:8} {len(ns):5d} golpes{'':33}{tag}")
        else:
            lines.append(f"  {ch:8} {s.count:5d} notas, mediana {midi_name(round(s.median)):4}, {s.pitches:3d} alturas, "
                         f"volumen {s.volume:4.1f}  {tag}")
    lines.append("sugerencia: " + " ".join(f"--{k} {','.join(v)}" for k, v in song.assign.items()) +
                 f" --bateria {','.join(song.drums)}")
    if song.groups:
        lines.append("batería:")
        for g in song.groups:
            item = song.assignment.get(g.key, {})
            role = item.get("lane") or "-"
            lines.append(f"  {g.key:22} {g.count:5d} golpes  {g.description:34} {role}{' (platillo)' if item.get('cymbal') else ''}")
    return lines


__all__ = ["Song", "load", "to_notes", "describe", "auto_assign", "suggest_assignment", "ROLES"]
