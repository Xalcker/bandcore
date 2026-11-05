"""Las notas del YM2151 (OPM), el FM de 4 operadores y 8 canales de los arcades (CPS1, System 16, OutRun,
Konami...) y del X68000.

Ningún juego toca este chip: sus canciones solo se arreglan para otra consola (`band export-notes`). De las
escrituras sale, por canal, cada nota con su altura, su volumen y su instrumento (el patch: los registros de sus
operadores cuando se presionó la tecla). El ruido (el operador C2 del canal 8 con el bit NE de $0F) se anota como
otro canal, "ruido", con su frecuencia de ruido como "altura".

Registros que importan (hoja de datos del YM2151):
    $08  tecla: bits 0-2 el canal, bits 3-6 los operadores (M1, C1, M2, C2); 0 = soltar
    $0F  ruido: bit 7 lo enciende (en el canal 8), bits 0-4 su frecuencia
    $20+c  bits 6-7 salidas izquierda y derecha (las dos en 0: el canal no se oye), bits 0-2 el algoritmo
    $28+c  KC: bits 4-6 la octava, bits 0-3 la nota (C#, D, D#, -, E, F, F#, -, G, G#, A, -, A#, B, C, -)
    $30+c  KF: bits 2-7, 64 pasos por semitono
    $40-$FF  los operadores: registro = base + operador * 8 + canal, operadores en el orden M1, M2, C1, C2
    $60-$7F  TL, la atenuación de cada operador (0.75 dB por paso)
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .chart import Note
from .vgm import SAMPLES_PER_FRAME, Vgm, frame_of, looped_writes

CHANNELS = 8
NOISE = "ruido"
CHANNEL_NAMES = [f"fm{c + 1}" for c in range(CHANNELS)] + [NOISE]
REFERENCE_CLOCK = 3579545  # con este reloj, KC = $4A es el La de 440 Hz
# Operadores que suenan (carriers) en cada algoritmo, en el orden de los registros (M1, M2, C1, C2)
CARRIERS = {0: (3,), 1: (3,), 2: (3,), 3: (3,), 4: (2, 3), 5: (1, 2, 3), 6: (1, 2, 3), 7: (0, 1, 2, 3)}
KEY_BITS = (0x08, 0x20, 0x10, 0x40)  # bit de cada operador (M1, M2, C1, C2) en $08
PITCH_SLACK = 0.75  # semitonos que la altura puede moverse (vibrato, portamento) sin que cuente como otra nota
SILENT_DB = 45.0  # una nota con sus carriers más atenuados que esto no se oye


@dataclass(frozen=True)
class FmNote(Note):
    volume: int = 15  # 0-15, del TL de sus carriers (15 = sin atenuación)


def kc_pitch(kc: int, kf: int = 0, clock: int = REFERENCE_CLOCK) -> float:
    """Altura MIDI (con decimales) de KC y KF, corregida por el reloj del chip (4 MHz suena casi 2 semitonos arriba)."""
    octave, code = (kc >> 4) & 7, kc & 0x0F
    index = (code >> 2) * 3 + min(code & 3, 2)  # los códigos 3, 7, 11 y 15 no existen: suenan como el anterior
    return 13 + 12 * octave + index + (kf >> 2) / 64 + 12 * math.log2(clock / REFERENCE_CLOCK)


def volume_of(tl: int) -> int:
    """Volumen 0-15 de una atenuación TL (0-127): 3 dB por paso, 0 desde SILENT_DB."""
    db = tl * 0.75
    return 0 if db >= SILENT_DB else max(1, 15 - round(db / 3))


class _Channel:
    def __init__(self) -> None:
        self.regs = bytearray(0x100)  # solo los de este canal: $20, $28, $30, $38 y sus operadores
        self.keyed = False
        self.start: int | None = None
        self.pitch = 0.0
        self.volume = 0
        self.channel = ""  # fm1-fm8 o ruido
        self.instrument = ""

    def carriers_tl(self) -> int:
        con = self.regs[0x20] & 7
        return min(self.regs[0x60 + op * 8] & 0x7F for op in CARRIERS[con])


class _Extractor:
    def __init__(self, clock: int) -> None:
        self.clock = clock
        self.ch = [_Channel() for _ in range(CHANNELS)]
        self.notes: dict[str, list[FmNote]] = {name: [] for name in CHANNEL_NAMES}
        self.noise = 0  # $0F
        self.patches: dict[bytes, int] = {}

    def pitch(self, c: int) -> float:
        regs = self.ch[c].regs
        if c == 7 and self.noise & 0x80:
            return float(self.noise & 0x1F)
        return kc_pitch(regs[0x28], regs[0x30], self.clock)

    def patch(self, c: int) -> str:
        """El número de patch: los registros de los operadores y el algoritmo (sin el volumen de los carriers)."""
        regs = self.ch[c].regs
        con = regs[0x20] & 7
        key = bytes([regs[0x20] & 0x3F, regs[0x38]]) + bytes(
            regs[base + op * 8] & (0x00 if base == 0x60 and op in CARRIERS[con] else 0xFF)
            for base in (0x40, 0x60, 0x80, 0xA0, 0xC0, 0xE0) for op in range(4))
        return f"p{self.patches.setdefault(key, len(self.patches))}"

    def name(self, c: int) -> str:
        return NOISE if c == 7 and self.noise & 0x80 else CHANNEL_NAMES[c]

    def close(self, c: int, frame: int) -> None:
        ch = self.ch[c]
        # Una nota de 0 cuadros no se oye: los drivers cambian la altura y sueltan la tecla en el mismo cuadro
        if ch.start is not None and ch.volume > 0 and frame > ch.start:
            self.notes[ch.channel].append(
                FmNote(ch.start, round(ch.pitch), frame - ch.start, instrument=ch.instrument, volume=ch.volume))
        ch.start = None

    def open(self, c: int, frame: int) -> None:
        self.close(c, frame)
        ch = self.ch[c]
        if not ch.regs[0x20] & 0xC0:  # sin salida: no se oye
            return
        ch.start, ch.pitch = frame, self.pitch(c)
        ch.volume = volume_of(ch.carriers_tl())
        ch.channel, ch.instrument = self.name(c), self.patch(c)

    def write(self, frame: int, reg: int, value: int) -> None:
        if reg == 0x08:
            c = value & 7
            on = bool(value & 0x78)
            ch = self.ch[c]
            if on and not ch.keyed:
                self.open(c, frame)
            elif not on and ch.keyed:
                self.close(c, frame)
            ch.keyed = on
        elif reg == 0x0F:
            if (value ^ self.noise) & 0x80 and self.ch[7].keyed:  # el canal 8 cambia entre FM y ruido
                self.noise = value
                self.open(7, frame)
            self.noise = value
        elif reg >= 0x20:
            c = reg & 7
            ch = self.ch[c]
            ch.regs[reg & 0xF8] = value
            if ch.start is None:
                return
            if frame == ch.start:  # el driver termina de preparar la nota en el cuadro en que la presiona
                ch.pitch = self.pitch(c)
                ch.volume = volume_of(ch.carriers_tl())
                ch.instrument = self.patch(c)
                return
            if (reg & 0xF8) in (0x28, 0x30) and not (c == 7 and self.noise & 0x80):
                pitch = self.pitch(c)
                if abs(pitch - ch.pitch) >= PITCH_SLACK and frame > ch.start:
                    self.open(c, frame)  # legato: otra nota sin soltar la tecla
            elif 0x60 <= reg < 0x80:
                ch.volume = max(ch.volume, volume_of(ch.carriers_tl()))


def extract_notes(vgm: Vgm, max_samples: int | None = None, index: int = 0) -> dict[str, list[FmNote]]:
    """Las notas de cada canal (fm1-fm8 y ruido) del YM2151 número `index`, hasta `max_samples` (repitiendo el bucle;
    sin decirlo, una vez hasta el final del archivo)."""
    max_samples = vgm.end_sample if max_samples is None else max_samples
    ex = _Extractor(vgm.clocks.get("ym2151", REFERENCE_CLOCK))
    for w in looped_writes(vgm, max_samples, "ym2151", index):
        ex.write(frame_of(w.sample), w.reg, w.value)
    end = (max_samples + SAMPLES_PER_FRAME // 2) // SAMPLES_PER_FRAME
    for c in range(CHANNELS):
        ex.close(c, end)
    return {name: sorted(ns, key=lambda n: n.frame) for name, ns in ex.notes.items()}
