"""Lector genérico de VGM: cualquier chip, sin interpretar lo que se escribe.

Un VGM guarda la música como escrituras a los registros de uno o más chips de sonido, con esperas en muestras
de 44.1 kHz. Este módulo lee `.vgm` y `.vgz` (el mismo formato comprimido con gzip) y entrega:

  - la cabecera: versión, el reloj de cada chip que trae (`Vgm.clocks`), duración, bucle y etiquetas GD3;
  - las escrituras a cualquier chip (`ChipWrite`), con su instante;
  - los bloques de datos (`Block`, comando $67) sin interpretar;
  - los eventos del DAC del YM2612 (`DacEvent`: $8n toca un byte del banco, $E0 salta a un lugar);
  - las escrituras a la memoria del SegaPCM ($C0), como `ChipWrite` con la dirección en `reg`;
  - los comandos de los streams de DAC ($90-$95, `StreamEvent`), con los que se tocan samples de un bloque
    (el OKIM6258 del X68000, por ejemplo);
  - los bloques de ROM de los chips de samples armados como la memoria del chip (`rom`);
  - los comandos que no se leen (`Other`: escrituras a RAM $68, otras memorias de chips $C1-$DF...).

Qué hacer con todo eso le toca a cada juego (GENBand, NGBand...) y a `detect.py`, que dice para qué juego sirve.

Referencia del formato: https://vgmrips.net/wiki/VGM_Specification
"""

from __future__ import annotations

import gzip
import struct
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

SAMPLE_RATE = 44100
SAMPLES_PER_FRAME = 735  # 44100 / 60

# Relojes de la cabecera: (posición, chip, versión mínima). Un campo que cae dentro de los datos (después del
# inicio que marca $34) no existe y vale 0. Antes de la versión 1.10, el reloj de $10 servía también para el
# YM2612 y el YM2151; aquí se reporta como "ym2413" y los comandos que sí aparecen deciden (`Vgm.chips`).
CLOCKS = (
    (0x0C, "sn76489", 0x100),
    (0x10, "ym2413", 0x100),
    (0x2C, "ym2612", 0x110),
    (0x30, "ym2151", 0x110),
    (0x38, "segapcm", 0x151),
    (0x40, "rf5c68", 0x151),
    (0x44, "ym2203", 0x151),
    (0x48, "ym2608", 0x151),
    (0x4C, "ym2610", 0x151),
    (0x50, "ym3812", 0x151),
    (0x54, "ym3526", 0x151),
    (0x58, "y8950", 0x151),
    (0x5C, "ymf262", 0x151),
    (0x60, "ymf278b", 0x151),
    (0x64, "ymf271", 0x151),
    (0x68, "ymz280b", 0x151),
    (0x6C, "rf5c164", 0x151),
    (0x70, "pwm", 0x151),
    (0x74, "ay8910", 0x151),
    (0x80, "gb_dmg", 0x161),
    (0x84, "nes_apu", 0x161),
    (0x88, "multipcm", 0x161),
    (0x8C, "upd7759", 0x161),
    (0x90, "okim6258", 0x161),
    (0x98, "okim6295", 0x161),
    (0x9C, "k051649", 0x161),
    (0xA0, "k054539", 0x161),
    (0xA4, "huc6280", 0x161),
    (0xA8, "c140", 0x161),
    (0xAC, "k053260", 0x161),
    (0xB0, "pokey", 0x161),
    (0xB4, "qsound", 0x161),
    (0xB8, "scsp", 0x171),
    (0xC0, "wonderswan", 0x171),
    (0xC4, "vsu", 0x171),
    (0xC8, "saa1099", 0x171),
    (0xCC, "es5503", 0x171),
    (0xD0, "es5506", 0x171),
    (0xD8, "x1_010", 0x171),
    (0xDC, "c352", 0x171),
    (0xE0, "ga20", 0x171),
)

# Comandos de escritura con dos bytes (registro y valor): comando -> (chip, puerto). $A1-$AF son el segundo chip
# de $51-$5F.
_REG_WRITES = {
    0x51: ("ym2413", 0),
    0x52: ("ym2612", 0),
    0x53: ("ym2612", 1),
    0x54: ("ym2151", 0),
    0x55: ("ym2203", 0),
    0x56: ("ym2608", 0),
    0x57: ("ym2608", 1),
    0x58: ("ym2610", 0),
    0x59: ("ym2610", 1),
    0x5A: ("ym3812", 0),
    0x5B: ("ym3526", 0),
    0x5C: ("y8950", 0),
    0x5D: ("ymz280b", 0),
    0x5E: ("ymf262", 0),
    0x5F: ("ymf262", 1),
}
_SECOND_WRITES = {cmd + 0x50: target for cmd, target in _REG_WRITES.items()}
# $A0 y $B0-$BF: el bit 7 del registro dice si es el segundo chip
_AA_WRITES = {
    0xA0: "ay8910",
    0xB0: "rf5c68",
    0xB1: "rf5c164",
    0xB3: "gb_dmg",  # registro = dirección - $FF10
    0xB4: "nes_apu",  # registro = dirección - $4000 ($20-$3E: FDS)
    0xB5: "multipcm",
    0xB6: "upd7759",
    0xB7: "okim6258",
    0xB8: "okim6295",
    0xB9: "huc6280",
    0xBA: "k053260",
    0xBB: "pokey",
    0xBC: "wonderswan",
    0xBD: "saa1099",
    0xBE: "es5506",
    0xBF: "ga20",
}


@dataclass(frozen=True)
class ChipWrite:
    sample: int  # instante (muestras de 44.1 kHz desde el inicio)
    chip: str  # nombre del chip, como en CLOCKS ("ym2612", "sn76489", "gb_dmg"...)
    reg: int  # registro (en el SN76489, 0: se escribe un solo byte)
    value: int
    port: int = 0  # puerto del chip (los YM de dos puertos); en el SN76489, 1 = estéreo de Game Gear ($4F)
    index: int = 0  # 0 = primer chip, 1 = segundo
    cmd: int = 0  # byte de comando del VGM


@dataclass(frozen=True)
class Block:
    """Un bloque de datos ($67): `type` según la especificación (0 = samples del YM2612, $81 = ADPCM-A...)."""

    sample: int
    type: int
    data: bytes


@dataclass(frozen=True)
class DacEvent:
    """$8n (`kind` = "play": sale un byte del banco de samples al DAC del YM2612; el VGM espera n después) o
    $E0 (`kind` = "seek": el puntero del banco salta a `offset`)."""

    sample: int
    kind: str
    offset: int = 0


@dataclass(frozen=True)
class StreamEvent:
    """Un comando de stream de DAC: `kind` = "setup" ($90: `args` = tipo de chip, puerto, comando), "data" ($91:
    bloque, tamaño del paso, base del paso), "freq" ($92: frecuencia), "start" ($93: posición en el banco, modo del
    largo, largo), "stop" ($94) o "block" ($95: número de bloque, banderas). El tipo de chip es su lugar en la
    cabecera (0 = SN76489, 3 = YM2151, $17 = OKIM6258...; bit 7 = segundo chip)."""

    sample: int
    kind: str
    stream: int
    args: tuple[int, ...] = ()


@dataclass(frozen=True)
class Other:
    """Un comando que este lector no interpreta (pero sabe cuánto mide)."""

    sample: int
    cmd: int
    pos: int  # byte del archivo donde empieza


@dataclass
class Vgm:
    version: int
    clocks: dict[str, int]  # chip -> reloj en Hz (sin los bits de banderas)
    flags: dict[str, int]  # chip -> bits 30 y 31 del reloj (bit 30: hay dos chips; bit 31: variante del chip)
    total_samples: int
    loop_sample: int | None  # dónde empieza el bucle (None = no tiene)
    loop_samples: int
    tags: dict[str, str]
    writes: list[ChipWrite] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    dac: list[DacEvent] = field(default_factory=list)
    other: list[Other] = field(default_factory=list)
    streams: list[StreamEvent] = field(default_factory=list)
    end_sample: int = 0  # instante del fin de los datos ($66); `total_samples` es lo que dice la cabecera
    loop_write: int | None = None  # índice en `writes` de la primera escritura del bucle (None = no tiene)
    header: bytes = b""  # la cabecera tal cual (hasta donde empiezan los datos), para los campos que no se leen aquí

    def header_word(self, offset: int) -> int:
        """Un campo de 32 bits de la cabecera (0 si la cabecera no llega ahí)."""
        return struct.unpack_from("<I", self.header, offset)[0] if offset + 4 <= len(self.header) else 0

    @property
    def title(self) -> str:
        return self.tags.get("title", "").strip()

    @property
    def game(self) -> str:
        return self.tags.get("game", "").strip()

    @property
    def author(self) -> str:
        return self.tags.get("author", "").strip()

    @property
    def used(self) -> Counter[str]:
        """Cuántas escrituras recibe cada chip (los golpes del DAC cuentan para el YM2612)."""
        count = Counter(w.chip for w in self.writes)
        if self.dac:
            count["ym2612"] += len(self.dac)
        return count

    def chips(self) -> list[str]:
        """Los chips que suenan: los que reciben escrituras, en el orden de la cabecera."""
        used = self.used
        order = [name for _, name, _ in CLOCKS]
        return sorted(used, key=lambda c: order.index(c) if c in order else len(order))

    @property
    def seconds(self) -> float:
        return self.total_samples / SAMPLE_RATE


def _gd3(data: bytes, offset: int) -> dict[str, str]:
    if not offset or data[offset : offset + 4] != b"Gd3 ":
        return {}
    length = struct.unpack_from("<I", data, offset + 8)[0]
    text = data[offset + 12 : offset + 12 + length].decode("utf-16-le", errors="replace")
    fields = text.split("\0")
    names = ["title", "title_jp", "game", "game_jp", "system", "system_jp", "author", "author_jp", "date", "ripper", "notes"]
    return {name: value for name, value in zip(names, fields) if value}


def _other_length(cmd: int, pos: int) -> int:
    """Bytes que ocupa un comando que no se interpreta (incluido el comando)."""
    if 0x20 <= cmd <= 0x3F or cmd in (0x4F, 0x50):
        return 2
    if 0x40 <= cmd <= 0x4E or 0x51 <= cmd <= 0x5F or 0xA0 <= cmd <= 0xBF:
        return 3
    if 0xC0 <= cmd <= 0xDF:
        return 4
    if 0xE0 <= cmd <= 0xFF:
        return 5
    if cmd == 0x64:
        return 4
    if cmd == 0x68:
        return 12
    if cmd in (0x90, 0x91, 0x95):
        return 5
    if cmd == 0x92:
        return 6
    if cmd == 0x93:
        return 11
    if cmd == 0x94:
        return 2
    raise ValueError(f"comando VGM desconocido ${cmd:02X} en el byte {pos:#x}")


def _stream(cmd: int, data: bytes, pos: int, sample: int) -> StreamEvent:
    stream = data[pos + 1]
    if cmd == 0x90:
        return StreamEvent(sample, "setup", stream, tuple(data[pos + 2 : pos + 5]))
    if cmd == 0x91:
        return StreamEvent(sample, "data", stream, tuple(data[pos + 2 : pos + 5]))
    if cmd == 0x92:
        return StreamEvent(sample, "freq", stream, struct.unpack_from("<I", data, pos + 2))
    if cmd == 0x93:
        return StreamEvent(sample, "start", stream, struct.unpack_from("<IBI", data, pos + 2))
    if cmd == 0x94:
        return StreamEvent(sample, "stop", stream)
    return StreamEvent(sample, "block", stream, struct.unpack_from("<HB", data, pos + 2))


def rom(vgm: Vgm, block_type: int, index: int = 0) -> bytes:
    """La ROM de un chip de samples ($80 SegaPCM, $8B OKIM6295, $8E K053260...) armada de sus bloques: cada bloque
    trae el tamaño total, dónde empieza y sus bytes. `index` 1 es la del segundo chip (bit 31 del tamaño)."""
    image = bytearray()
    for block in vgm.blocks:
        if block.type != block_type or len(block.data) < 8:
            continue
        size, start = struct.unpack_from("<II", block.data)
        if (size >> 31) != index:
            continue
        payload = block.data[8:]
        end = start + len(payload)
        if len(image) < max(size & 0x7FFFFFFF, end):
            image.extend(bytes(max(size & 0x7FFFFFFF, end) - len(image)))
        image[start:end] = payload
    return bytes(image)


def parse_vgm(raw: bytes, name: str = "VGM") -> Vgm:
    """Interpreta los bytes de un VGM (con o sin gzip). Lanza ValueError si no es un VGM o está dañado."""
    try:
        return _parse(raw, name)
    except (struct.error, IndexError) as exc:
        raise ValueError(f"{name}: archivo VGM truncado o dañado ({exc})") from exc
    except (OSError, EOFError) as exc:  # gzip dañado
        raise ValueError(f"{name}: no se pudo descomprimir ({exc})") from exc


def read_vgm(path: Path | str) -> Vgm:
    """Lee un `.vgm` o `.vgz`."""
    path = Path(path)
    return parse_vgm(path.read_bytes(), str(path))


def _parse(raw: bytes, name: str) -> Vgm:
    data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
    if data[:4] != b"Vgm " or len(data) < 0x40:
        raise ValueError(f"{name}: no es un archivo VGM")
    version = struct.unpack_from("<I", data, 0x08)[0]
    gd3_offset = struct.unpack_from("<I", data, 0x14)[0]
    total = struct.unpack_from("<I", data, 0x18)[0]
    loop_offset = struct.unpack_from("<I", data, 0x1C)[0]
    loop_samples = struct.unpack_from("<I", data, 0x20)[0]
    start = 0x40
    if version >= 0x150:
        rel = struct.unpack_from("<I", data, 0x34)[0]
        if rel:
            start = 0x34 + rel

    clocks: dict[str, int] = {}
    flags: dict[str, int] = {}
    for offset, chip, min_version in CLOCKS:
        if version < min_version or offset + 4 > min(start, len(data)):
            continue
        value = struct.unpack_from("<I", data, offset)[0]
        if value & 0x3FFFFFFF:
            clocks[chip] = value & 0x3FFFFFFF
            if value >> 30:
                flags[chip] = value >> 30
    loop_pos = 0x1C + loop_offset if loop_offset else None

    vgm = Vgm(version, clocks, flags, 0, None, loop_samples, _gd3(data, 0x14 + gd3_offset if gd3_offset else 0),
              header=bytes(data[: min(start, len(data))]))
    waits = {0x62: 735, 0x63: 882}
    sample = 0
    pos = start
    while pos < len(data):
        if loop_pos is not None and pos == loop_pos:
            vgm.loop_sample = sample
            vgm.loop_write = len(vgm.writes)
        cmd = data[pos]
        if cmd == 0x66:
            break
        if cmd == 0x61:
            sample += struct.unpack_from("<H", data, pos + 1)[0]
            pos += 3
        elif cmd in waits:
            sample += waits[cmd]
            pos += 1
        elif 0x70 <= cmd <= 0x7F:
            sample += (cmd & 0x0F) + 1
            pos += 1
        elif cmd == 0x64:  # cambia cuánto esperan $62 o $63
            if data[pos + 1] in waits:
                waits[data[pos + 1]] = struct.unpack_from("<H", data, pos + 2)[0]
            pos += 4
        elif cmd in (0x50, 0x30, 0x4F, 0x3F):  # SN76489 (el $4F/$3F es el estéreo de Game Gear)
            port = 1 if cmd in (0x4F, 0x3F) else 0
            index = 1 if cmd in (0x30, 0x3F) else 0
            vgm.writes.append(ChipWrite(sample, "sn76489", 0, data[pos + 1], port, index, cmd))
            pos += 2
        elif cmd in _REG_WRITES or cmd in _SECOND_WRITES:
            index = 1 if cmd in _SECOND_WRITES else 0
            chip, port = (_SECOND_WRITES if index else _REG_WRITES)[cmd]
            vgm.writes.append(ChipWrite(sample, chip, data[pos + 1], data[pos + 2], port, index, cmd))
            pos += 3
        elif cmd in _AA_WRITES:
            aa = data[pos + 1]
            vgm.writes.append(ChipWrite(sample, _AA_WRITES[cmd], aa & 0x7F, data[pos + 2], 0, aa >> 7, cmd))
            pos += 3
        elif 0x80 <= cmd <= 0x8F:
            vgm.dac.append(DacEvent(sample, "play"))
            sample += cmd & 0x0F
            pos += 1
        elif cmd == 0xE0:
            vgm.dac.append(DacEvent(sample, "seek", struct.unpack_from("<I", data, pos + 1)[0]))
            pos += 5
        elif cmd == 0xC0:  # SegaPCM: escritura a su memoria (dirección de 16 bits)
            addr = struct.unpack_from("<H", data, pos + 1)[0]
            vgm.writes.append(ChipWrite(sample, "segapcm", addr & 0x7FFF, data[pos + 3], 0, addr >> 15, cmd))
            pos += 4
        elif 0x90 <= cmd <= 0x95:
            vgm.streams.append(_stream(cmd, data, pos, sample))
            pos += _other_length(cmd, pos)
        elif cmd == 0x67:
            if data[pos + 1] != 0x66:
                raise ValueError(f"{name}: bloque de datos mal formado en el byte {pos:#x}")
            size = struct.unpack_from("<I", data, pos + 3)[0] & 0x7FFFFFFF
            payload = data[pos + 7 : pos + 7 + size]
            if len(payload) < size:
                raise ValueError(f"{name}: bloque de datos truncado en el byte {pos:#x}")
            vgm.blocks.append(Block(sample, data[pos + 2], bytes(payload)))
            pos += 7 + size
        else:
            length = _other_length(cmd, pos)
            vgm.other.append(Other(sample, cmd, pos))
            pos += length
    vgm.end_sample = sample
    vgm.total_samples = total or sample
    return vgm


def looped_writes(vgm: Vgm, max_samples: int, chip: str | None = None, index: int = 0):
    """Las escrituras como las toca el archivo: hasta el fin y, si tiene bucle, otra vez desde el punto de repetición
    (en el instante exacto, en muestras), mientras el instante sea menor que `max_samples`. Con `chip`, solo las de ese
    chip (y ese `index`: 0 = primer chip)."""

    def keep(w: ChipWrite) -> bool:
        return chip is None or (w.chip == chip and w.index == index)

    for w in vgm.writes:
        if w.sample >= max_samples:
            return
        if keep(w):
            yield w
    if vgm.loop_write is None or vgm.loop_sample is None:
        return
    period = vgm.end_sample - vgm.loop_sample
    loop = [w for w in vgm.writes[vgm.loop_write :] if keep(w)]
    if period <= 0 or not loop:  # una vuelta sin esperas no avanza: se termina
        return
    shift = period
    while True:
        for w in loop:
            sample = w.sample + shift
            if sample >= max_samples:
                return
            yield ChipWrite(sample, w.chip, w.reg, w.value, w.port, w.index, w.cmd)
        shift += period


def frame_of(sample: int) -> int:
    """El cuadro (a 60 por segundo) más cercano a un instante."""
    return (sample + SAMPLES_PER_FRAME // 2) // SAMPLES_PER_FRAME


def unrolled_writes(vgm, frames: int | None = None) -> list:
    """Las escrituras con su cuadro, repitiendo el bucle hasta llenar `frames` cuadros (si se pide).

    Sin `frames` se toca una sola vez, hasta el final del archivo. Sirve para cualquier objeto con `writes` (cada una
    con `sample`), `total_samples`, `loop_sample` y `loop_samples`: el `Vgm` de aquí o el de cada juego."""
    out = [(frame_of(w.sample), w) for w in vgm.writes]
    if frames is None:
        return out
    if vgm.loop_sample is not None and vgm.loop_samples:
        loop = [w for w in vgm.writes if w.sample >= vgm.loop_sample]
        offset = vgm.total_samples
        period = vgm.total_samples - vgm.loop_sample
        while loop and period > 0 and frame_of(offset) < frames:
            out += [(frame_of(w.sample - vgm.loop_sample + offset), w) for w in loop]
            offset += period
    return [(f, w) for f, w in out if f < frames]
