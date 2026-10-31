"""Pruebas del APU del NES emulado y del proyecto de FamiStudio fiel a lo que sonó."""

import struct

from bandcore import famistudio, nesapu
from bandcore.vgm import parse_vgm


def nes_vgm(body: bytes, total: int) -> bytes:
    header = bytearray(0x100)
    header[0:4] = b"Vgm "
    struct.pack_into("<I", header, 0x08, 0x161)
    struct.pack_into("<I", header, 0x18, total)
    struct.pack_into("<I", header, 0x34, 0x100 - 0x34)
    struct.pack_into("<I", header, 0x84, 1789772)
    return bytes(header) + body + b"\x66"


def w(reg: int, value: int) -> bytes:
    return bytes([0xB4, reg - 0x4000, value])


def wait(samples: int) -> bytes:
    return bytes([0x61]) + struct.pack("<H", samples)


# Pulso 1: La 440 con volumen constante 12 y duty 25 %; luego Do con envolvente de caída (duty 50 %).
# Ruido: un golpe de periodo 12 con volumen constante 10, y el triángulo una nota.
BODY = (w(0x4015, 0x0F) + w(0x4000, 0x70 | 12) + w(0x4001, 0x08) + w(0x4002, 253) + w(0x4003, 0x08)
        + w(0x4008, 0xFF) + w(0x400A, 200) + w(0x400B, 0x08)
        + w(0x400C, 0x30 | 10) + w(0x400E, 12) + w(0x400F, 0x08)
        + wait(22050)
        + w(0x4000, 0x80 | 3) + w(0x4002, 213) + w(0x4003, 0x08)
        + wait(22050))


def test_grabacion_de_un_vgm():
    rec, fds = nesapu.record_vgm(parse_vgm(nes_vgm(BODY, 44100)), 44100)
    assert fds == 0 and rec.frames == 60
    notes = nesapu.notes(rec)
    assert [n.pitch for n in notes["Square1"]] == [69, 72]
    assert rec.tracks["Square1"].vol[5] == 12 and rec.tracks["Square1"].duty[5] == 1
    assert len(notes["Triangle"]) == 1 and len(notes["Noise"]) == 1


def test_proyecto_fiel(tmp_path):
    rec, _ = nesapu.record_vgm(parse_vgm(nes_vgm(BODY, 44100)), 44100)
    path = tmp_path / "x.txt"
    info = famistudio.write_recording_project(rec, path, "Prueba", "NESBand")
    text = path.read_text(encoding="utf-8")
    # Dos instrumentos de pulso: volumen constante 12 con duty 1, y la caída (15, 14, ...) con duty 2
    pulses = text.split('Instrument Name="Pulso ')[1:]
    assert len(pulses) == 2 and info["dpcm_lost"] == 0
    assert 'Values="12"' in pulses[0] and 'DutyCycle" Length="1" Values="1"' in pulses[0]
    assert 'Values="15,14' in pulses[1] or 'Values="14,' in pulses[1]
    assert 'DutyCycle" Length="1" Values="2"' in pulses[1]
    # El golpe de ruido usa la nota del periodo 12 (A#2 en alguna octava) y su volumen
    noise = text.split('Channel Type="Noise"')[1].split('Channel Type="DPCM"')[0]
    assert f'Value="{famistudio.noise_note(12)}"' in noise
    assert 'Instrument Name="Ruido 1"' in text and 'Instrument Name="Triangulo 1"' in text
