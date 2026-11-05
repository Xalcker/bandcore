"""Los golpes de los chips de samples que acompañan al YM2151 en los arcades, para la batería.

Cada chip dice de otra forma cuándo empieza un sample:

    OKIM6295 (CPS1)       $B8, registro 0: un byte con el bit 7 elige la frase (el sample) y el siguiente dice
                          en qué voces suena (bits 4-7); sin el bit 7, apaga las voces de los bits 3-6.
    SegaPCM (OutRun)      $C0: el registro de control de un canal ($86 + 8 x canal) con el bit 0 en 0 lo arranca
                          desde la dirección de $84/$85 y el banco de los bits altos del control.
    K053260 (Konami)      $BA: el registro $28 enciende los canales (bits 0-3, al pasar de 0 a 1); cada canal
                          tiene su dirección en $0C-$0E + 8 x canal.
    OKIM6258 (X68000)     streams de DAC ($95): cada golpe toca un bloque de datos completo.

Un golpe es una `Note` de chart cuya "altura" es el número del sample (en orden de aparición) y cuyo instrumento es
la llave del sample ("oki:11", "segapcm:40c1d"...). De cada sample se decodifica el audio (`Sample`) para sugerir
su papel: un bombo cruza el cero pocas veces por segundo; una tarola o un platillo, muchas.
"""

from __future__ import annotations

from dataclasses import dataclass

from .chart import Note
from .vgm import SAMPLES_PER_FRAME, Vgm, frame_of, looped_writes, rom

CHIPS = ("okim6295", "segapcm", "k053260", "okim6258")
CHANNEL = {"okim6295": "oki", "segapcm": "segapcm", "k053260": "k053260", "okim6258": "adpcm"}
MAX_HIT_FRAMES = 120  # un golpe dura lo que su sample, hasta 2 segundos
ANALYSIS_SECONDS = 0.25  # cuánto de cada sample se mira para decir cómo suena


@dataclass(frozen=True)
class Sample:
    key: str
    seconds: float
    crossings: float  # cruces por cero por segundo (bajo en un bombo, alto en una tarola o un platillo)


# --- ADPCM de OKI (el mismo en el MSM6295 y el MSM6258) -----------------------------------------------------

_STEPS = [int(16.0 * (11 / 10) ** i) for i in range(49)]
_INDEX_SHIFT = (-1, -1, -1, -1, 2, 4, 6, 8)


def oki_adpcm(data: bytes, high_first: bool = True, limit: int | None = None) -> list[int]:
    out: list[int] = []
    signal, index = 0, 0
    for byte in data:
        for nibble in ((byte >> 4, byte & 15) if high_first else (byte & 15, byte >> 4)):
            step = _STEPS[index]
            diff = ((2 * (nibble & 7) + 1) * step) // 8
            signal = max(-2048, min(2047, signal - diff if nibble & 8 else signal + diff))
            index = max(0, min(48, index + _INDEX_SHIFT[nibble & 7]))
            out.append(signal)
        if limit is not None and len(out) >= limit:
            break
    return out


_K053260_DELTAS = (0, 1, 2, 4, 8, 16, 32, 64, -128, -64, -32, -16, -8, -4, -2, -1)


def k053260_adpcm(data: bytes) -> list[int]:
    out, signal = [], 0
    for byte in data:
        for nibble in (byte & 15, byte >> 4):
            signal = (signal + _K053260_DELTAS[nibble] + 128) % 256 - 128
            out.append(signal)
    return out


def crossings_per_second(signal: list[int], rate: float) -> float:
    """Cruces por cero por segundo, alrededor del promedio (el ADPCM se desvía del cero)."""
    if len(signal) < 2 or rate <= 0:
        return 0.0
    mean = sum(signal) / len(signal)
    signs = [s >= mean for s in signal]
    return sum(1 for a, b in zip(signs, signs[1:]) if a != b) * rate / len(signal)


def _sample(key: str, signal: list[int], rate: float, length: int | None = None) -> Sample:
    head = signal[: max(2, int(rate * ANALYSIS_SECONDS))]
    return Sample(key, (length if length is not None else len(signal)) / rate if rate else 0.0,
                  crossings_per_second(head, rate))


# --- Golpes de cada chip -------------------------------------------------------------------------------------


class _Hits:
    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.notes: list[Note] = []
        self.ids: dict[str, int] = {}
        self.samples: dict[str, Sample] = {}

    def hit(self, sample: int, key: str, seconds: float = 0.1) -> None:
        frame = frame_of(sample)
        self.notes.append(Note(frame, self.ids.setdefault(key, len(self.ids)),
                               max(1, min(MAX_HIT_FRAMES, round(seconds * 60))), instrument=key))


def _okim6295(vgm: Vgm, max_samples: int, index: int) -> _Hits:
    out = _Hits(CHANNEL["okim6295"])
    data = rom(vgm, 0x8B, index)
    pin7 = (vgm.header_word(0x98) >> 31) & 1
    rate = vgm.clocks.get("okim6295", 1000000) / (132 if pin7 else 165)
    bank, pending = 0, None

    def sample_of(phrase: int) -> tuple[str, float]:
        key = f"oki:{bank:x}:{phrase}" if bank else f"oki:{phrase}"
        base = bank << 18
        entry = data[base + phrase * 8 : base + phrase * 8 + 6]
        if key not in out.samples and len(entry) == 6:
            start = base + (int.from_bytes(entry[:3], "big") & 0x3FFFF)
            end = base + (int.from_bytes(entry[3:], "big") & 0x3FFFF)
            if end > start:
                body = data[start : end + 1]
                out.samples[key] = _sample(key, oki_adpcm(body, True, int(rate * ANALYSIS_SECONDS) + 2), rate,
                                           len(body) * 2)
        return key, out.samples[key].seconds if key in out.samples else 0.1

    for w in looped_writes(vgm, max_samples, "okim6295", index):
        if w.reg == 0x0F:
            bank = w.value
        elif w.reg != 0:
            continue
        elif pending is not None:
            if w.value >> 4:
                key, seconds = sample_of(pending)
                out.hit(w.sample, key, seconds)
            pending = None
        elif w.value & 0x80:
            pending = w.value & 0x7F
    return out


def _segapcm(vgm: Vgm, max_samples: int, index: int) -> _Hits:
    out = _Hits(CHANNEL["segapcm"])
    data = rom(vgm, 0x80, index)
    interface = vgm.header_word(0x3C)
    shift = interface & 0x0F or 12
    mask = (interface >> 16) & 0xFF or 0x70
    rate_base = vgm.clocks.get("segapcm", 4000000) / 128
    regs = bytearray(0x100)
    moved = [False] * 16  # el canal recibió dirección nueva desde su último arranque
    last = [(-10, False)] * 16  # (cuadro, se oía) del último arranque de cada canal
    for w in looped_writes(vgm, max_samples, "segapcm", index):
        if w.reg >= 0x100:
            continue
        ch = (w.reg >> 3) & 0x0F
        old = regs[w.reg]
        regs[w.reg] = w.value
        if w.reg & 0x87 in (0x84, 0x85):
            moved[ch] = True
        # Arranca al encenderse (el bit 0 pasa a 0) o al volver a encenderse con otra dirección; el driver también
        # reescribe el control de un canal que ya suena (para el banco o el bucle) y eso no es un golpe
        if w.reg & 0x87 != 0x86 or w.value & 1 or not (old & 1 or moved[ch]):
            continue
        moved[ch] = False
        # Algunos drivers (Super Hang-On) rearrancan el canal en cada cuadro con el volumen en 0 hasta que toca: el
        # golpe es el primer arranque que se oye
        frame = frame_of(w.sample)
        audible = bool(regs[0x02 + 8 * ch] or regs[0x03 + 8 * ch])
        previous = last[ch]
        last[ch] = (frame, audible)
        if not audible or (previous[1] and frame - previous[0] <= 1):
            continue
        bank = (w.value & mask) << shift
        start = bank + (regs[0x85 + 8 * ch] << 8 | regs[0x84 + 8 * ch])
        key = f"segapcm:{start:x}"
        if key not in out.samples:
            end = bank + ((regs[0x06 + 8 * ch] + 1) << 8)
            rate = rate_base * max(1, regs[0x07 + 8 * ch]) / 256
            body = data[start:end] if end > start else data[start : start + int(rate)]
            out.samples[key] = _sample(key, [b - 128 for b in body], rate)
        out.hit(w.sample, key, out.samples[key].seconds)
    return out


def _k053260(vgm: Vgm, max_samples: int, index: int) -> _Hits:
    out = _Hits(CHANNEL["k053260"])
    data = rom(vgm, 0x8E, index)
    clock = vgm.clocks.get("k053260", 3579545)
    regs = bytearray(0x40)
    for w in looped_writes(vgm, max_samples, "k053260", index):
        if w.reg >= 0x40:
            continue
        old = regs[0x28]
        regs[w.reg] = w.value
        if w.reg != 0x28:
            continue
        for ch in range(4):
            if not (w.value >> ch) & 1 or (old >> ch) & 1:
                continue
            base = 0x08 + 8 * ch
            start = regs[base + 6] << 16 | regs[base + 5] << 8 | regs[base + 4]
            size = regs[base + 3] << 8 | regs[base + 2]
            adpcm = bool(regs[0x2A] & (0x10 << ch))
            key = f"k053260:{start:x}"
            if key not in out.samples:
                pitch = (regs[base + 1] & 0x0F) << 8 | regs[base]
                rate = clock / max(1, 0x1000 - pitch)
                body = data[start : start + size]
                signal = k053260_adpcm(body) if adpcm else [b - 256 if b >= 128 else b for b in body]
                out.samples[key] = _sample(key, signal, rate)
            out.hit(w.sample, key, out.samples[key].seconds)
    return out


def _okim6258(vgm: Vgm, max_samples: int, index: int) -> _Hits:
    out = _Hits(CHANNEL["okim6258"])
    chip_of: dict[int, int] = {}  # stream -> tipo de chip
    bank_of: dict[int, int] = {}  # stream -> tipo de bloque de datos
    rate_of: dict[int, float] = {}
    for event in vgm.streams:
        if event.sample >= max_samples:
            break
        if event.kind == "setup":
            chip_of[event.stream] = event.args[0]
        elif event.kind == "data":
            bank_of[event.stream] = event.args[0]
        elif event.kind == "freq":
            rate_of[event.stream] = float(event.args[0])
        elif event.kind == "block" and chip_of.get(event.stream) == 0x17 + (0x80 if index else 0):
            blocks = [b for b in vgm.blocks if b.type == bank_of.get(event.stream, 4)]
            block = event.args[0]
            if block >= len(blocks):
                continue
            key = f"adpcm:{block}"
            rate = rate_of.get(event.stream, 7812.5)
            if key not in out.samples:
                out.samples[key] = _sample(key, oki_adpcm(blocks[block].data, False), rate)
            out.hit(event.sample, key, out.samples[key].seconds)
    # Los streams no se repiten con `looped_writes`: con bucle, se repiten aquí
    if vgm.loop_sample is not None and out.notes:
        period_frames = (vgm.end_sample - vgm.loop_sample) / SAMPLES_PER_FRAME
        loop = [n for n in out.notes if n.frame >= frame_of(vgm.loop_sample)]
        end_frame = frame_of(max_samples)
        shift = period_frames
        while loop and period_frames > 0:
            extra = [Note(round(n.frame + shift), n.pitch, n.duration, instrument=n.instrument) for n in loop]
            extra = [n for n in extra if n.frame < end_frame]
            if not extra:
                break
            out.notes += extra
            shift += period_frames
    return out


def extract_hits(vgm: Vgm, max_samples: int) -> tuple[dict[str, list[Note]], dict[str, Sample]]:
    """Los golpes de cada chip de samples que suena (canal "oki", "segapcm", "k053260" o "adpcm") y cómo suena cada
    sample (llave -> `Sample`)."""
    used = vgm.used
    channels: dict[str, list[Note]] = {}
    samples: dict[str, Sample] = {}
    for chip, read in (("okim6295", _okim6295), ("segapcm", _segapcm), ("k053260", _k053260), ("okim6258", _okim6258)):
        if not used[chip] and not (chip == "okim6258" and vgm.streams):
            continue
        hits = read(vgm, max_samples, 0)
        if hits.notes:
            channels[hits.channel] = sorted(hits.notes, key=lambda n: n.frame)
            samples.update(hits.samples)
    return channels, samples
