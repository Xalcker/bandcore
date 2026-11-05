"""Pruebas de las canciones de arcade con YM2151: notas FM (ym2151), golpes de los chips de samples (pcm), qué canal
es cada instrumento y la batería (arcade), y `band export-notes` / `band channels`."""

import json
import struct

from test_vgm import YM2151, make_vgm

from bandcore import arcade, notes, pcm, ym2151
from bandcore.chart import Note
from bandcore.cli import main
from bandcore.vgm import parse_vgm

FRAME = 735
OKI, SEGAPCM, K053260 = 0x98, 0x38, 0xAC


def wait(frames: int) -> bytes:
    out = b""
    for _ in range(frames):
        out += b"\x62"
    return out


def ym(reg: int, value: int) -> bytes:
    return bytes([0x54, reg, value])


def patch(c: int, tl: int = 0) -> bytes:
    """Canal `c` audible: salida a los dos lados, algoritmo 7 (los 4 operadores suenan) con atenuación `tl`."""
    return ym(0x20 + c, 0xC7) + b"".join(ym(0x60 + op * 8 + c, tl) for op in range(4))


def note(c: int, kc: int, frames: int, gap: int = 0) -> bytes:
    return ym(0x28 + c, kc) + ym(0x08, 0x78 | c) + wait(frames) + ym(0x08, c) + wait(gap)


def song(body: bytes, clock: int = 3579545, **kw) -> object:
    return parse_vgm(make_vgm(body, {YM2151: clock, **kw.pop("clocks", {})}, **kw))


# --- ym2151 --------------------------------------------------------------------------------------------------


def test_kc_pitch():
    assert ym2151.kc_pitch(0x4A) == 69  # La 440 con el reloj de 3.58 MHz
    assert ym2151.kc_pitch(0x4E) == 72 and ym2151.kc_pitch(0x50) == 73  # Do 5 y Do# 5
    assert ym2151.kc_pitch(0x4A, 0x80) == 69.5  # KF: 64 pasos por semitono
    assert abs(ym2151.kc_pitch(0x4A, 0, 4_000_000) - 70.92) < 0.01  # 4 MHz suena casi 2 semitonos arriba


def test_notes_volume_and_silent_channels():
    body = (patch(0) + patch(1, tl=127) + ym(0x22, 0x07)  # el canal 3 sin salida
            + note(0, 0x4A, 10, 2) + note(0, 0x4E, 5) + note(1, 0x4A, 4) + note(2, 0x4A, 4))
    got = ym2151.extract_notes(song(body))
    assert [(n.frame, n.duration, n.pitch, n.volume) for n in got["fm1"]] == [(0, 10, 69, 15), (12, 5, 72, 15)]
    assert got["fm2"] == [] and got["fm3"] == []  # atenuado del todo / sin salida


def test_legato_and_same_frame_changes():
    body = (patch(0) + ym(0x08, 0x78) + ym(0x28, 0x4A) + wait(6)  # la altura llega después de la tecla, mismo cuadro
            + ym(0x28, 0x4E) + wait(4)  # legato: otra nota sin soltar
            + ym(0x28, 0x50) + ym(0x08, 0x00) + wait(2))  # cambia y suelta en el mismo cuadro: no hay nota de 0
    got = ym2151.extract_notes(song(body))["fm1"]
    assert [(n.frame, n.duration, n.pitch) for n in got] == [(0, 6, 69), (6, 4, 72)]


def test_noise_is_its_own_channel():
    body = patch(7) + ym(0x0F, 0x80 | 25) + note(7, 0x4A, 3, 1) + ym(0x0F, 0) + note(7, 0x4A, 3)
    got = ym2151.extract_notes(song(body))
    assert [(n.frame, n.pitch) for n in got["ruido"]] == [(0, 25)]
    assert [(n.frame, n.pitch) for n in got["fm8"]] == [(4, 69)]


# --- pcm -----------------------------------------------------------------------------------------------------


def rom_block(kind: int, data: bytes, start: int = 0) -> bytes:
    return bytes([0x67, 0x66, kind]) + struct.pack("<I", 8 + len(data)) + struct.pack("<II", len(data), start) + data


def test_okim6295_phrases_and_sound():
    # Frase 1: un "bombo" (ADPCM que cambia poco) y frase 2: ruido (ADPCM que salta)
    table = bytearray(0x400)
    low, noisy = bytes([0x11] * 400), bytes([0x7F, 0xF7] * 200)
    for phrase, (start, data) in ((1, (0x400, low)), (2, (0x400 + len(low), noisy))):
        table[phrase * 8 : phrase * 8 + 6] = (start.to_bytes(3, "big") + (start + len(data) - 1).to_bytes(3, "big"))
    data = bytes(table) + low + noisy
    body = (rom_block(0x8B, data) + bytes([0xB8, 0, 0x81, 0xB8, 0, 0x10]) + wait(5)  # frase 1 en la voz 1
            + bytes([0xB8, 0, 0x82, 0xB8, 0, 0x20]) + wait(5) + bytes([0xB8, 0, 0x78]))  # frase 2; apaga todo
    hits, samples = pcm.extract_hits(song(b"\x54\x08\x00" + body, clocks={OKI: 1_000_000}), 44100)
    assert [(n.frame, n.instrument) for n in hits["oki"]] == [(0, "oki:1"), (5, "oki:2")]
    assert samples["oki:1"].crossings < samples["oki:2"].crossings


def test_segapcm_counts_audible_starts_only():
    def write(addr: int, value: int) -> bytes:
        return bytes([0xC0]) + struct.pack("<H", addr) + bytes([value])

    start = write(0x84, 0x00) + write(0x85, 0x10) + write(0x86, 0x00)
    body = (rom_block(0x80, bytes(range(256)) * 32)
            + write(0x02, 0) + write(0x03, 0) + start + wait(1) + start + wait(1)  # rearmado en silencio
            + write(0x02, 0x40) + start + wait(1) + start + wait(5)  # suena (una vez, aunque se rearme)
            + write(0x86, 0x02) + wait(1) + write(0x86, 0x00))  # el control de un canal que suena: no es golpe
    hits, _ = pcm.extract_hits(song(b"\x54\x08\x00" + body, clocks={SEGAPCM: 4_000_000}), 44100 * 2)
    assert [(n.frame, n.instrument) for n in hits["segapcm"]] == [(2, "segapcm:1000")]


def test_k053260_key_on_edges():
    def write(reg: int, value: int) -> bytes:
        return bytes([0xBA, reg, value])

    body = (rom_block(0x8E, bytes(4096)) + write(0x0C, 0x00) + write(0x0D, 0x02) + write(0x0A, 0x80)
            + write(0x28, 0x01) + wait(3) + write(0x28, 0x01) + wait(3) + write(0x28, 0x00) + write(0x28, 0x01))
    hits, _ = pcm.extract_hits(song(b"\x54\x08\x00" + body, clocks={K053260: 3579545}), 44100)
    assert [(n.frame, n.instrument) for n in hits["k053260"]] == [(0, "k053260:200"), (6, "k053260:200")]


def test_okim6258_stream_blocks():
    block = bytes([0x67, 0x66, 0x04]) + struct.pack("<I", 64) + bytes(64)
    body = (block + block + bytes([0x90, 0, 0x17, 0, 1, 0x91, 0, 4, 1, 0, 0x92, 0]) + struct.pack("<I", 7813)
            + bytes([0x95, 0]) + struct.pack("<HB", 1, 0) + wait(4) + bytes([0x95, 0]) + struct.pack("<HB", 0, 0))
    hits, samples = pcm.extract_hits(song(b"\x54\x08\x00" + body), 44100)
    assert [(n.frame, n.instrument) for n in hits["adpcm"]] == [(0, "adpcm:1"), (4, "adpcm:0")]
    assert set(samples) == {"adpcm:0", "adpcm:1"}


# --- arcade --------------------------------------------------------------------------------------------------


def line(pitches: list[int], start: int = 0, step: int = 8, length: int = 6) -> list[Note]:
    return [Note(start + i * step, p, length) for i, p in enumerate(pitches)]


def test_auto_assign_skips_drums_and_doubles():
    melody = [72, 74, 76, 77, 79, 81, 83, 84] * 4
    notes_ = {
        "fm1": line(melody),
        "fm2": line([p - 12 for p in melody], start=3),  # el eco de fm1, una octava abajo y 3 cuadros después
        "fm3": line([60, 62, 64, 65, 67, 69] * 6),
        "fm4": line([36, 38, 40, 41, 43] * 6),
        "fm5": line([40] * 30),  # siempre la misma altura: bombo FM
        "fm6": [Note(i * 8 + d, 50 - d, 1) for i in range(30) for d in range(3)],  # barridos: golpes FM
    }
    assert arcade.auto_assign(notes_) == {"bajo": ["fm4"], "guitarra": ["fm1"], "teclado": ["fm3"]}
    assert arcade.drum_channels(notes_, {"fm1", "fm3", "fm4"}) == ["fm5", "fm6"]
    assert [n.frame for n in arcade.strikes("fm6", notes_["fm6"])][:3] == [0, 8, 16]


def test_suggest_assignment_from_samples():
    def group(key, count, seconds, crossings):
        return arcade.Group(key, "oki", count, 0, pcm.Sample(key, seconds, crossings))

    groups = [group("oki:hat", 900, 0.1, 7000), group("oki:kick", 400, 0.2, 200), group("oki:snare", 300, 0.3, 2500),
              group("oki:crash", 40, 1.5, 8000), group("oki:tom", 50, 0.4, 600), group("oki:poco", 2, 0.4, 600)]
    got = arcade.suggest_assignment(groups)
    assert got["oki:kick"]["lane"] == "kick" and got["oki:snare"]["lane"] == "snare"
    assert got["oki:hat"] == {"lane": "tom1", "cymbal": True} and got["oki:crash"] == {"lane": "tom3", "cymbal": True}
    assert got["oki:tom"]["lane"] in ("tom2", "tom3") and got["oki:poco"]["lane"] is None


def test_suggest_assignment_fm_drums():
    groups = [arcade.Group("fm8:D1", "fm8", 200, 26), arcade.Group("fm8:E4", "fm8", 150, 64),
              arcade.Group("fm8:A5", "fm8", 300, 81), arcade.Group("ruido:20", "ruido", 30, 20)]
    got = arcade.suggest_assignment(groups)
    assert got["fm8:D1"]["lane"] == "kick" and got["fm8:E4"]["lane"] == "snare"
    assert got["fm8:A5"] == {"lane": "tom1", "cymbal": True}


def arcade_vgm() -> bytes:
    """Una canción: melodía en fm1, bajo en fm2, golpes de OKI (frase 1 grave, frase 2 ruidosa)."""
    table = bytearray(0x400)
    low, noisy = bytes([0x11] * 400), bytes([0x7F, 0xF7] * 200)
    for phrase, (start, data) in ((1, (0x400, low)), (2, (0x400 + len(low), noisy))):
        table[phrase * 8 : phrase * 8 + 6] = (start.to_bytes(3, "big") + (start + len(data) - 1).to_bytes(3, "big"))
    body = rom_block(0x8B, bytes(table) + low + noisy) + patch(0) + patch(1)
    melody, bass = [0x4A, 0x4C, 0x4E, 0x50, 0x52, 0x54], [0x20, 0x21, 0x22, 0x24, 0x25]
    for i in range(30):
        body += ym(0x28, melody[i % 6]) + ym(0x29, bass[i % 5]) + ym(0x08, 0x78) + ym(0x08, 0x79)
        body += bytes([0xB8, 0, 0x81 + i % 2, 0xB8, 0, 0x10]) + wait(6) + ym(0x08, 0) + ym(0x08, 1) + wait(2)
    return make_vgm(body, {YM2151: 3579545, OKI: 1_000_000}, gd3={"title": "Prueba", "game": "Arcade"})


def test_export_notes_and_drums_file(tmp_path, capsys):
    source = tmp_path / "x.vgm"
    source.write_bytes(arcade_vgm())
    out, drums = tmp_path / "notas.json", tmp_path / "drums.json"
    assert main(["export-notes", str(source), "--out", str(out), "--drums", str(drums)]) == 0
    data = notes.read(out)
    assert data["title"] == "Prueba" and data["game"] == "Arcade"
    assert len(data["channels"]["Square1"]) == 30 and data["channels"]["Square1"][0][2] == 69
    assert data["channels"]["Triangle"][0][2] == 37 and data["channels"]["Square2"] == []
    assert {role for _, role, _ in data["drums"]} == {"kick", "snare"}
    groups = json.loads(drums.read_text())["groups"]
    assert groups == {"oki:1": {"lane": "kick", "cymbal": False}, "oki:2": {"lane": "snare", "cymbal": False}}
    # Corregido a mano: el drums.json manda
    groups["oki:2"] = {"lane": "tom1", "cymbal": True}
    drums.write_text(json.dumps({"version": 1, "groups": groups}))
    assert main(["export-notes", str(source), "--out", str(out), "--drums", str(drums), "--guitarra", "fm2"]) == 0
    data = notes.read(out)
    assert {(role, cymbal) for _, role, cymbal in data["drums"]} == {("kick", False), ("tom1", True)}
    assert data["channels"]["Square1"][0][2] == 37
    capsys.readouterr()
    assert main(["channels", str(source)]) == 0
    text = capsys.readouterr().out
    assert "--bajo fm2 --guitarra fm1" in text and "oki:1" in text
    assert main(["export-notes", str(source), "--out", str(out), "--bajo", "fm9"]) == 1  # canal que no existe
