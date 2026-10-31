"""El APU del NES emulado: de las escrituras a $4000-$4017, el estado de cada canal en cada cuadro y sus notas.

Lo usan GBBand (NSF y VGM del NES a su IR) y NESBand (VGM del NES a un proyecto de FamiStudio). Las escrituras vienen
de donde sea: de la rutina de reproducción de un NSF corrida en un 6502 (GBBand), o de un VGM (`record_vgm`).

Qué emula: envolventes (constantes y de caída), contadores de longitud, contador lineal del triángulo, barrido de los
pulsos y los cuatro pasos del secuenciador de cuadros. El DPCM solo se registra (cuándo empieza cada sample y qué
suena) para clasificar los golpes. Los chips de sonido extra (VRC6, FDS, N163...) no.

Todo se mide en cuadros del NES (60.1 Hz).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .chart import Note

NES_CPU_HZ = 1_789_773
NES_FPS = 60.0988
CYCLES_PER_FRAME = 29780
TONAL = ("Square1", "Square2", "Triangle")
LENGTHS = [10, 254, 20, 2, 40, 4, 80, 6, 160, 8, 60, 10, 14, 12, 26, 14, 12, 16, 24, 18, 48, 20, 96, 22, 192, 24, 72, 26, 16, 28, 32, 30]
NOISE_PERIODS = [4, 8, 16, 32, 64, 96, 128, 160, 202, 254, 380, 508, 762, 1016, 2034, 4068]
DPCM_RATES = [428, 380, 340, 320, 286, 254, 226, 214, 190, 160, 142, 128, 106, 84, 72, 54]
BLOCK_NES_RAM = 0xC2  # bloque de datos de un VGM: dirección (2 bytes) y los bytes que van ahí (los samples DPCM)


def hz_midi(freq: float) -> float:
    return 69 + 12 * math.log2(freq / 440.0)


@dataclass
class Track:
    """El estado de un canal tonal en cada cuadro. `freq` = 0 significa silencio.
    `trig` marca el cuadro en que arranca una nota (reinicia la envolvente y la fase)."""

    freq: list[float]
    vol: list[int]
    duty: list[int]
    trig: list[bool]

    @classmethod
    def silent(cls, frames: int) -> "Track":
        return cls([0.0] * frames, [0] * frames, [2] * frames, [False] * frames)


@dataclass
class Envelope:
    const: bool = True
    loop: bool = False
    vol: int = 0
    start: bool = False
    divider: int = 0
    decay: int = 0

    def clock(self) -> None:
        if self.start:
            self.start, self.decay, self.divider = False, 15, self.vol
        elif self.divider == 0:
            self.divider = self.vol
            if self.decay > 0:
                self.decay -= 1
            elif self.loop:
                self.decay = 15
        else:
            self.divider -= 1

    @property
    def out(self) -> int:
        return self.vol if self.const else self.decay


@dataclass
class Pulse:
    index: int
    env: Envelope = field(default_factory=Envelope)
    duty: int = 0
    timer: int = 0
    length: int = 0
    enabled: bool = False
    sweep_on: bool = False
    sweep_period: int = 0
    sweep_negate: bool = False
    sweep_shift: int = 0
    sweep_reload: bool = False
    sweep_div: int = 0
    trig: bool = False

    def target(self) -> int:
        change = self.timer >> self.sweep_shift
        return self.timer - change - (1 if self.index == 0 else 0) if self.sweep_negate else self.timer + change

    def muted(self) -> bool:
        return self.timer < 8 or self.target() > 0x7FF

    def clock_sweep(self) -> None:
        if self.sweep_div == 0 and self.sweep_on and self.sweep_shift and not self.muted():
            self.timer = self.target()
        if self.sweep_div == 0 or self.sweep_reload:
            self.sweep_div, self.sweep_reload = self.sweep_period, False
        else:
            self.sweep_div -= 1

    def audible(self) -> bool:
        return self.length > 0 and not self.muted()


class Apu:
    def __init__(self) -> None:
        self.pulses = [Pulse(0), Pulse(1)]
        self.tri_control = False
        self.tri_reload = 0
        self.tri_linear = 0
        self.tri_reload_flag = False
        self.tri_timer = 0
        self.tri_length = 0
        self.tri_enabled = False
        self.tri_trig = False
        self.noise_env = Envelope()
        self.noise_halt = False
        self.noise_mode = False
        self.noise_period = 0
        self.noise_length = 0
        self.noise_enabled = False
        self.noise_trig = False
        self.dpcm_rate = 0
        self.dpcm_addr = 0
        self.dpcm_len = 0
        self.dpcm_starts: list[tuple[int, int, int]] = []  # (dirección, bytes, tasa) de los samples que arrancaron en el cuadro

    def write(self, addr: int, value: int) -> None:
        if 0x4000 <= addr <= 0x4007:
            p = self.pulses[(addr - 0x4000) >> 2]
            reg = addr & 3
            if reg == 0:
                p.duty = value >> 6
                p.env.loop = bool(value & 0x20)
                p.env.const = bool(value & 0x10)
                p.env.vol = value & 0x0F
            elif reg == 1:
                p.sweep_on, p.sweep_period = bool(value & 0x80), (value >> 4) & 7
                p.sweep_negate, p.sweep_shift, p.sweep_reload = bool(value & 8), value & 7, True
            elif reg == 2:
                p.timer = (p.timer & 0x700) | value
            else:
                p.timer = (p.timer & 0xFF) | (value & 7) << 8
                if p.enabled:
                    p.length = LENGTHS[value >> 3]
                p.env.start = True
                p.trig = True
        elif addr == 0x4008:
            self.tri_control, self.tri_reload = bool(value & 0x80), value & 0x7F
        elif addr == 0x400A:
            self.tri_timer = (self.tri_timer & 0x700) | value
        elif addr == 0x400B:
            self.tri_timer = (self.tri_timer & 0xFF) | (value & 7) << 8
            if self.tri_enabled:
                self.tri_length = LENGTHS[value >> 3]
            self.tri_reload_flag = True
            self.tri_trig = True
        elif addr == 0x400C:
            self.noise_halt = bool(value & 0x20)
            self.noise_env.loop = self.noise_halt
            self.noise_env.const = bool(value & 0x10)
            self.noise_env.vol = value & 0x0F
        elif addr == 0x400E:
            self.noise_mode, self.noise_period = bool(value & 0x80), value & 0x0F
        elif addr == 0x400F:
            if self.noise_enabled:
                self.noise_length = LENGTHS[value >> 3]
            self.noise_env.start = True
            self.noise_trig = True
        elif addr == 0x4010:
            self.dpcm_rate = value & 0x0F
        elif addr == 0x4012:
            self.dpcm_addr = 0xC000 + value * 64
        elif addr == 0x4013:
            self.dpcm_len = value * 16 + 1
        elif addr == 0x4015:
            for p, bit in zip(self.pulses, (0, 1)):
                p.enabled = bool(value >> bit & 1)
                if not p.enabled:
                    p.length = 0
            self.tri_enabled = bool(value & 4)
            if not self.tri_enabled:
                self.tri_length = 0
            self.noise_enabled = bool(value & 8)
            if not self.noise_enabled:
                self.noise_length = 0
            if value & 0x10:
                self.dpcm_starts.append((self.dpcm_addr, self.dpcm_len, self.dpcm_rate))

    def status(self) -> int:
        return (self.pulses[0].length > 0) | (self.pulses[1].length > 0) << 1 | (self.tri_length > 0) << 2 | (self.noise_length > 0) << 3

    def clock_quarter(self) -> None:
        for p in self.pulses:
            p.env.clock()
        self.noise_env.clock()
        if self.tri_reload_flag:
            self.tri_linear = self.tri_reload
        elif self.tri_linear > 0:
            self.tri_linear -= 1
        if not self.tri_control:
            self.tri_reload_flag = False

    def clock_half(self) -> None:
        for p in self.pulses:
            if not p.env.loop and p.length > 0:
                p.length -= 1
            p.clock_sweep()
        if not self.tri_control and self.tri_length > 0:
            self.tri_length -= 1
        if not self.noise_halt and self.noise_length > 0:
            self.noise_length -= 1

    def finish_frame(self) -> None:
        """Los pasos 2 a 4 del secuenciador de cuadros (60 Hz): el primero (un reloj de envolvente) se aplica antes de
        registrar el estado del cuadro, para que lo que escribió la rutina de reproducción ya cuente (la envolvente
        arranca en 15 y el contador lineal del triángulo se carga en ese primer reloj)."""
        self.clock_quarter()
        self.clock_half()
        self.clock_quarter()
        self.clock_quarter()
        self.clock_half()


# ---- Grabación cuadro por cuadro ------------------------------------------------------------------------------------

@dataclass
class Recording:
    frames: int
    tracks: dict[str, Track]
    noise_hits: list[tuple[int, int, int, bool]]  # (cuadro, índice de periodo, vol, modo corto)
    dpcm_hits: list[tuple[int, float, int]]  # (cuadro, cruces por cero, duración en cuadros)
    noise_active: list[int]  # volumen del ruido en cada cuadro (0 = callado)


def dpcm_zcr(read, start: int, length: int) -> float:
    """Cruces por cero por bit del sample (modulación delta de 1 bit): bajo = grave (bombo), alto = agudo o ruido.
    `read(addr)` lee la memoria donde está el sample."""
    bits = []
    for i in range(min(length, 400)):
        byte = read(start + i if start + i <= 0xFFFF else start)
        bits += [byte >> k & 1 for k in range(8)]
    flips = sum(1 for a, b in zip(bits, bits[1:]) if a != b)
    return flips / max(len(bits), 1)


def record(apu: Apu, frames: int, play, read) -> Recording:
    """Graba cuadro por cuadro el estado de cada canal del APU. `play(f)` escribe los registros del cuadro `f` (la
    rutina de reproducción de un NSF, o las escrituras de un VGM) y `read(addr)` lee la memoria (los samples DPCM)."""
    tracks = {c: Track.silent(frames) for c in TONAL}
    noise_hits: list[tuple[int, int, int, bool]] = []
    dpcm_hits: list[tuple[int, float, int]] = []
    noise_active = [0] * frames
    for f in range(frames):
        play(f)
        # estado de cada canal al inicio del cuadro (después de lo que escribió la rutina de reproducción y del
        # primer reloj del secuenciador)
        apu.clock_quarter()
        for p, name in zip(apu.pulses, ("Square1", "Square2")):
            track = tracks[name]
            if p.audible() and p.length > 0 and p.env.out > 0:
                track.freq[f] = NES_CPU_HZ / (16 * (p.timer + 1))
                track.vol[f] = p.env.out
                track.duty[f] = p.duty
            track.trig[f] = p.trig
            p.trig = False
        track = tracks["Triangle"]
        if apu.tri_length > 0 and apu.tri_linear > 0 and apu.tri_timer >= 2:
            track.freq[f] = NES_CPU_HZ / (32 * (apu.tri_timer + 1))
            track.vol[f] = 15
        track.trig[f] = apu.tri_trig
        apu.tri_trig = False
        if apu.noise_length > 0 and apu.noise_env.out > 0:
            noise_active[f] = apu.noise_env.out
        if apu.noise_trig:
            noise_hits.append((f, apu.noise_period, apu.noise_env.out, apu.noise_mode))
            apu.noise_trig = False
        for addr, length, rate in apu.dpcm_starts:
            dur = max(1, round(length * 8 * DPCM_RATES[rate] / CYCLES_PER_FRAME))
            dpcm_hits.append((f, dpcm_zcr(read, addr, length), dur))
        apu.dpcm_starts.clear()
        apu.finish_frame()
    return Recording(frames, tracks, noise_hits, dpcm_hits, noise_active)


def record_vgm(vgm, total_samples: int) -> tuple[Recording, int]:
    """Graba un VGM del NES (`bandcore.vgm.Vgm`) durante `total_samples` (repite el bucle si hace falta). Las escrituras
    de cada cuadro del NES se aplican antes de grabarlo. Devuelve la grabación y cuántas escrituras al canal del
    Famicom Disk System se ignoraron."""
    from .vgm import SAMPLE_RATE, looped_writes

    frames = max(1, int(total_samples / SAMPLE_RATE * NES_FPS))
    writes = list(looped_writes(vgm, total_samples, "nes_apu"))
    memory = bytearray(0x10000)
    for block in vgm.blocks:
        if block.type == BLOCK_NES_RAM and len(block.data) > 2:
            start = block.data[0] | block.data[1] << 8
            data = block.data[2 : 2 + 0x10000 - start]
            memory[start : start + len(data)] = data
    apu = Apu()
    pos = 0
    fds = 0

    def play(f: int) -> None:
        nonlocal pos, fds
        end = (f + 1) * SAMPLE_RATE / NES_FPS
        while pos < len(writes) and writes[pos].sample < end:
            w = writes[pos]
            if w.reg <= 0x17:
                apu.write(0x4000 + w.reg, w.value)
            else:
                fds += 1
            pos += 1

    return record(apu, frames, play, memory.__getitem__), fds


# ---- Notas ---------------------------------------------------------------------------------------------------------

def tonal_notes(track: Track) -> list[Note]:
    """Las notas de un canal: una por arranque o por cambio de altura sostenido (el vibrato no cuenta como notas)."""
    notes: list[Note] = []
    start = pitch = None
    candidate: tuple[int, int] | None = None  # (cuadro, altura) de un posible cambio
    for f in range(len(track.freq)):
        active = track.freq[f] > 0 and track.vol[f] > 0
        if not active:
            if start is not None:
                notes.append(Note(start, pitch, f - start))
                start = None
            candidate = None
            continue
        p = round(hz_midi(track.freq[f]))
        if start is None or track.trig[f]:
            if start is not None:
                notes.append(Note(start, pitch, f - start))
            start, pitch, candidate = f, p, None
        elif abs(p - pitch) >= 2:
            if candidate and candidate[1] == p:
                notes.append(Note(start, pitch, candidate[0] - start))
                start, pitch, candidate = candidate[0], p, None
            else:
                candidate = (f, p)
        else:
            candidate = None
    if start is not None:
        notes.append(Note(start, pitch, len(track.freq) - start))
    return notes


def noise_duration(rec: Recording, frame: int) -> int:
    """Cuadros que suena un golpe de ruido (hasta que se calla, como mucho 60)."""
    duration = 1
    while frame + duration < rec.frames and rec.noise_active[frame + duration] > 0 and duration < 60:
        duration += 1
    return duration


def notes(rec: Recording) -> dict[str, list[Note]]:
    """Las notas de los 5 canales. En el ruido la altura dice el periodo (más agudo = periodo más corto) y el modo; en
    el DPCM, los cruces por cero del sample (grave = bombo)."""
    out: dict[str, list[Note]] = {name: tonal_notes(rec.tracks[name]) for name in TONAL}
    out["Noise"] = []
    for f, period, vol, short in rec.noise_hits:
        pitch = 24 + (15 - period) * 3 + (12 if short else 0)
        out["Noise"].append(Note(f, pitch, noise_duration(rec, f)))
    out["DPCM"] = [Note(f, 24 + int(zcr * 60), dur) for f, zcr, dur in rec.dpcm_hits]
    return out
