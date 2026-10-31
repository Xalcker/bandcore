"""Pruebas del lector genérico de VGM y de la detección del juego destino."""

import gzip
import json
import struct

import pytest

from bandcore import detect
from bandcore.cli import main
from bandcore.vgm import frame_of, looped_writes, parse_vgm, unrolled_writes

# Posiciones de los relojes en la cabecera
SN, YM2612, YM2151, YM2610, GB, NES = 0x0C, 0x2C, 0x30, 0x4C, 0x80, 0x84


def make_vgm(body: bytes, clocks: dict[int, int] | None = None, version: int = 0x171, loop_at: int | None = None,
             loop_samples: int = 0, total: int = 0, gd3: dict[str, str] | None = None) -> bytes:
    header = bytearray(0x100)
    header[0:4] = b"Vgm "
    struct.pack_into("<I", header, 0x08, version)
    struct.pack_into("<I", header, 0x18, total)
    if loop_at is not None:
        struct.pack_into("<I", header, 0x1C, 0x100 + loop_at - 0x1C)
        struct.pack_into("<I", header, 0x20, loop_samples)
    struct.pack_into("<I", header, 0x34, 0x100 - 0x34)
    for offset, clock in (clocks or {}).items():
        struct.pack_into("<I", header, offset, clock)
    out = bytes(header) + body + b"\x66"
    if gd3:
        names = ["title", "title_jp", "game", "game_jp", "system", "system_jp", "author", "author_jp", "date", "ripper", "notes"]
        text = "\0".join(gd3.get(n, "") for n in names) + "\0"
        payload = text.encode("utf-16-le")
        struct.pack_into("<I", header, 0x14, len(out) - 0x14)
        out = bytes(header) + body + b"\x66" + b"Gd3 " + struct.pack("<II", 0x100, len(payload)) + payload
    return out


def test_reads_writes_of_several_chips_and_waits():
    body = bytes([
        0x52, 0x28, 0xF0,  # YM2612 puerto 0
        0x50, 0x9F,  # PSG
        0x62,
        0x53, 0xA0, 0x12,  # YM2612 puerto 1
        0xB3, 0x12, 0x80,  # Game Boy: NR12 ($FF12)
        0xB4, 0x80 | 0x15, 0x0F,  # NES APU, segundo chip: $4015
        0xA8, 0x07, 0x38,  # segundo YM2610, puerto 0
        0x61, 0xE8, 0x03,
        0x7F,
    ])
    vgm = parse_vgm(make_vgm(body, {YM2612: 7670453, SN: 3579545}))
    got = [(w.sample, w.chip, w.port, w.index, w.reg, w.value) for w in vgm.writes]
    assert got == [
        (0, "ym2612", 0, 0, 0x28, 0xF0),
        (0, "sn76489", 0, 0, 0, 0x9F),
        (735, "ym2612", 1, 0, 0xA0, 0x12),
        (735, "gb_dmg", 0, 0, 0x12, 0x80),
        (735, "nes_apu", 0, 1, 0x15, 0x0F),
        (735, "ym2610", 0, 1, 0x07, 0x38),
    ]
    assert vgm.total_samples == 735 + 1000 + 16
    assert vgm.clocks == {"sn76489": 3579545, "ym2612": 7670453}
    assert vgm.chips() == ["sn76489", "ym2612", "ym2610", "gb_dmg", "nes_apu"]


def test_vgz_equals_vgm():
    raw = make_vgm(bytes([0x58, 0x07, 0x38]))
    assert parse_vgm(gzip.compress(raw)).writes == parse_vgm(raw).writes


def test_clock_flags_and_fields_past_the_header_are_ignored():
    vgm = parse_vgm(make_vgm(b"", {YM2610: 0x80000000 | 8_000_000, NES: 0x80000000 | 1789772}))
    assert vgm.clocks["ym2610"] == 8_000_000 and vgm.flags["ym2610"] == 2
    assert vgm.flags["nes_apu"] == 2
    # Versión 1.51 con datos desde $80: el reloj de Game Boy no existe aunque haya bytes ahí
    raw = bytearray(make_vgm(b"", {GB: 4194304}, version=0x151))
    struct.pack_into("<I", raw, 0x34, 0x100 - 0x34)
    assert "gb_dmg" not in parse_vgm(bytes(raw)).clocks


def test_loop_and_unrolled_writes():
    body = bytes([0x58, 0x00, 0x01, 0x62, 0x58, 0x01, 0x02, 0x62])
    vgm = parse_vgm(make_vgm(body, loop_at=4, loop_samples=735, total=1470))
    assert vgm.loop_sample == 735
    assert [f for f, _ in unrolled_writes(vgm, 5)] == [0, 1, 2, 3, 4]
    assert frame_of(735) == 1 and frame_of(367) == 0 and frame_of(368) == 1


def test_looped_writes_follow_the_file():
    # Escritura A en 0, marca de bucle, B en 0 (mismo instante: no se repite A), espera 100, C en 100, espera 50
    body = bytes([0xB3, 0x12, 0xA0, 0xB3, 0x13, 0xB0, 0x61, 100, 0, 0xB3, 0x14, 0xC0, 0x61, 50, 0, 0x50, 0x9F])
    vgm = parse_vgm(make_vgm(body, {GB: 4194304}, loop_at=3, loop_samples=150, total=150))
    assert vgm.loop_sample == 0 and vgm.loop_write == 1 and vgm.end_sample == 150
    got = [(w.sample, w.reg) for w in looped_writes(vgm, 400, "gb_dmg")]
    assert got == [(0, 0x12), (0, 0x13), (100, 0x14), (150, 0x13), (250, 0x14), (300, 0x13)]
    assert [w.sample for w in looped_writes(vgm, 1000)][:5] == [0, 0, 100, 150, 150]  # con el PSG del final
    no_loop = parse_vgm(make_vgm(body, {GB: 4194304}))
    assert [w.sample for w in looped_writes(no_loop, 10**6, "gb_dmg")] == [0, 0, 100]


def test_dac_blocks_and_other_commands():
    block = bytes([0x67, 0x66, 0x00]) + struct.pack("<I", 3) + b"\x80\x90\xA0"
    body = block + bytes([0xE0, 2, 0, 0, 0, 0x81, 0x82, 0x93] + [0] * 10 + [0xC0, 0, 0, 0])
    vgm = parse_vgm(make_vgm(body))
    assert [(b.type, b.data) for b in vgm.blocks] == [(0, b"\x80\x90\xA0")]
    assert [(d.sample, d.kind, d.offset) for d in vgm.dac] == [(0, "seek", 2), (0, "play", 0), (1, "play", 0)]
    assert [o.cmd for o in vgm.other] == [0x93, 0xC0]
    assert vgm.chips() == ["ym2612"]  # los golpes del DAC cuentan como YM2612


def test_wait_override():
    vgm = parse_vgm(make_vgm(bytes([0x64, 0x62, 0x6F, 0x03, 0x62, 0x63])))  # $62 ahora espera 879
    assert vgm.total_samples == 879 + 882


def test_gd3_tags():
    vgm = parse_vgm(make_vgm(b"\x50\x9F", gd3={"title": "Cancion", "game": "Juego", "system": "Sega Master System"}))
    assert vgm.tags == {"title": "Cancion", "game": "Juego", "system": "Sega Master System"}


@pytest.mark.parametrize("raw", [b"", b"nope" * 20, make_vgm(b"\x58\x00")[:0x102], make_vgm(b"\x67\x00")])
def test_invalid_input_raises(raw):
    with pytest.raises(ValueError):
        parse_vgm(raw)


# --- Detección ------------------------------------------------------------------------------------------


def targets(raw: bytes) -> tuple[str, list[tuple[str, str, bool]]]:
    report = detect.analyze(parse_vgm(raw))
    return report.system, [(t.game, t.mode, t.ready) for t in report.targets]


def test_genesis_is_native_for_genband():
    system, got = targets(make_vgm(bytes([0x52, 0x28, 0xF0, 0x50, 0x9F]), {YM2612: 7670453, SN: 3579545}))
    assert system == "Mega Drive / Genesis"
    assert got[0] == ("GENBand", "nativo", True)
    assert {g for g, m, _ in got if m == "arreglo"} == {"NGBand", "GBBand", "NESBand"}


def test_neo_geo_is_native_for_ngband():
    system, got = targets(make_vgm(bytes([0x58, 0x28, 0xF1]), {YM2610: 8_000_000}))
    assert system == "Neo Geo" and got[0] == ("NGBand", "nativo", True)


def test_game_boy_and_nes_are_pending():
    system, got = targets(make_vgm(bytes([0xB3, 0x12, 0x80]), {GB: 4194304}))
    assert system.startswith("Game Boy") and got[0] == ("GBBand", "nativo", True)
    system, got = targets(make_vgm(bytes([0xB4, 0x15, 0x0F]), {NES: 1789772}))
    assert system.startswith("NES") and got[:2] == [("NESBand", "nativo", False), ("GBBand", "arreglo", False)]


def test_unknown_chip_has_no_target():
    report = detect.analyze(parse_vgm(make_vgm(bytes([0xBB, 0x00, 0x10]))))  # POKEY
    assert report.targets == [] and report.lost == ["pokey"]


def test_cli_inspect_json(tmp_path, capsys):
    path = tmp_path / "x.vgz"
    path.write_bytes(gzip.compress(make_vgm(bytes([0x58, 0x28, 0xF1, 0x62]), {YM2610: 8_000_000}, gd3={"title": "Uno"})))
    assert main(["inspect", str(path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["system"] == "Neo Geo" and data[0]["tags"]["title"] == "Uno"
    assert main(["inspect", str(path)]) == 0
    assert "NGBand   nativo   listo" in capsys.readouterr().out
    assert main(["inspect", str(tmp_path / "no-existe.vgm")]) == 1
