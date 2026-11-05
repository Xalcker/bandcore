"""Pruebas de `band convert`: el plan de pasos según el origen y el destino, y su ejecución (con un corredor falso en
lugar de los juegos)."""

import json

import pytest
from test_arcade import arcade_vgm
from test_vgm import GB, YM2610, YM2612, make_vgm

from bandcore import convert
from bandcore.cli import main


@pytest.fixture
def games(tmp_path):
    root = tmp_path / "juegos"
    for game in convert.TARGETS.values():
        (root / game / "src" / "songs").mkdir(parents=True)
        (root / game / "pyproject.toml").write_text("")
    return root


def source(tmp_path, name, body, clocks):
    path = tmp_path / name
    path.write_bytes(make_vgm(body, clocks))
    return path


def plan(src, target, games, tmp_path, **opts):
    base = {k: None for k in ("title", "game", "duration", "track", "guitarra", "teclado", "bajo", "bateria", "drums")}
    flags = {k: opts.pop(k) for k in ("timbre", "import_song", "replace") if k in opts}
    return convert.make_plan(src, target, "prueba", games, tmp_path / "trabajo", {**base, **opts}, **flags)


def commands(p):
    return [(s.cwd.name if s.cwd else None, s.cmd[:4] if s.cmd else s.description.split()[0]) for s in p.steps]


def test_native_import(games, tmp_path):
    genesis = source(tmp_path, "g.vgm", bytes([0x52, 0x28, 0xF0]), {YM2612: 7670453})
    p = plan(genesis, "gen", games, tmp_path, duration=40.5, guitarra="fm1", drums="x.json")
    assert p.mode == "nativo" and len(p.steps) == 1
    cmd = p.steps[0].cmd
    assert cmd[:4] == ["uv", "run", "genband", "import-song"] and cmd[5:7] == ["--name", "prueba"]
    assert cmd[7:] == ["--duration", "40.5", "--guitarra", "fm1"]  # sin --drums: import-song no lo entiende
    assert p.song_dir == games / "GENBand" / "src" / "songs" / "prueba"


def test_arrangements_run_in_each_repo(games, tmp_path):
    genesis = source(tmp_path, "g.vgm", bytes([0x52, 0x28, 0xF0]), {YM2612: 7670453})
    p = plan(genesis, "nes", games, tmp_path, duration=40.5)
    assert commands(p) == [("GENBand", ["uv", "run", "genband", "export-notes"]), (None, "bandcore"),
                           ("NESBand", ["uv", "run", "nesband", "import-song"])]
    export = p.steps[0].cmd
    assert export[export.index("--duration") + 1] == "40"  # genband export-notes pide un entero
    gameboy = source(tmp_path, "gb.vgm", bytes([0xB3, 0x12, 0x80]), {GB: 4194304})
    p = plan(gameboy, "gen", games, tmp_path, timbre="B")
    assert commands(p) == [("GBBand", ["uv", "run", "gbband", "export-notes"]),
                           ("GENBand", ["uv", "run", "python", "audio/arreglo_fm.py"])]
    assert p.steps[1].cmd[-3:] == ["--timbre", "B", "--importar"]
    neogeo = source(tmp_path, "ng.vgm", bytes([0x58, 0x28, 0xF1]), {YM2610: 8_000_000})
    p = plan(neogeo, "ng", games, tmp_path)
    assert p.mode == "nativo"
    p = plan(gameboy, "ng", games, tmp_path)
    assert [s.cwd.name if s.cwd else None for s in p.steps] == ["GBBand", None, "NGBand", "NGBand"]
    assert p.steps[3].cmd[-2:] == ["--duration", convert.NOTES_SECONDS]


def test_other_sources(games, tmp_path):
    nsf = tmp_path / "x.nsf"
    nsf.write_bytes(b"NESM\x1a")
    assert plan(nsf, "nes", games, tmp_path).mode == "nativo"
    assert plan(nsf, "gb", games, tmp_path).mode == "nativo"  # gbband import-song arregla el NES solo
    p = plan(nsf, "ng", games, tmp_path, track=3)
    assert p.steps[0].cmd[2:4] == ["gbband", "export-notes"] and p.steps[0].cmd[-2:] == ["--track", "3"]
    midi = tmp_path / "x.mid"
    midi.write_bytes(b"MThd")
    with pytest.raises(ValueError, match="no conozco"):
        plan(midi, "nes", games, tmp_path)


def test_guards(games, tmp_path):
    genesis = source(tmp_path, "g.vgm", bytes([0x52, 0x28, 0xF0]), {YM2612: 7670453})
    (games / "NESBand" / "src" / "songs" / "prueba").mkdir()
    with pytest.raises(ValueError, match="--reemplazar"):
        plan(genesis, "nes", games, tmp_path)
    assert plan(genesis, "nes", games, tmp_path, replace=True).steps
    assert plan(genesis, "nes", games, tmp_path, import_song=False).song_dir is None
    with pytest.raises(ValueError, match="tal cual"):
        plan(genesis, "gen", games, tmp_path, import_song=False)
    with pytest.raises(ValueError, match="inválido"):
        convert.make_plan(genesis, "nes", "Mal Nombre", games, tmp_path, {})
    with pytest.raises(ValueError, match="destino"):
        plan(genesis, "snes", games, tmp_path)
    (games / "GENBand" / "pyproject.toml").unlink()
    with pytest.raises(ValueError, match="no encontré GENBand"):
        plan(genesis, "gb", games, tmp_path)


def test_execute_ym2151_to_neo_geo(games, tmp_path):
    arcade = tmp_path / "arcade.vgm"
    arcade.write_bytes(arcade_vgm())
    p = plan(arcade, "ng", games, tmp_path)
    ran = []

    def runner(cmd, cwd):
        ran.append((cwd.name, cmd))
        if "ngband_tools.famistudio" in cmd:
            open(cmd[-1], "wb").close()
        return 0

    convert.execute(p, runner, echo=lambda text: None)
    data = json.loads((p.work / "notas.json").read_text())
    assert data["title"] == "Prueba" and (p.work / "arreglo.txt").exists()
    assert [cwd for cwd, _ in ran] == ["NGBand", "NGBand"]
    duration = ran[1][1][ran[1][1].index("--duration") + 1]
    assert duration == f"{data['frames'] / 60 + 0.5:.1f}"  # la duración de las notas, no los 120 s del bucle


def test_execute_reports_failures(games, tmp_path):
    genesis = source(tmp_path, "g.vgm", bytes([0x52, 0x28, 0xF0]), {YM2612: 7670453})
    p = plan(genesis, "nes", games, tmp_path)
    with pytest.raises(RuntimeError, match="falló el paso 1"):
        convert.execute(p, lambda cmd, cwd: 1, echo=lambda text: None)
    with pytest.raises(RuntimeError, match="no dejó"):  # export-notes "terminó" sin escribir notas.json
        convert.execute(p, lambda cmd, cwd: 0, echo=lambda text: None)


def test_cli_plan(games, tmp_path, capsys):
    arcade = tmp_path / "arcade.vgm"
    arcade.write_bytes(arcade_vgm())
    assert main(["convert", str(arcade), "--to", "todos", "--name", "prueba", "--juegos", str(games), "--plan",
                 "--wav", "--work", str(tmp_path / "w")]) == 0
    out = capsys.readouterr().out
    assert out.count("== arcade.vgm") == 4 and "bandcore.player_wav" in out and "arreglo_fm.py" in out
    assert main(["convert", str(arcade), "--to", "nes", "--name", "prueba", "--juegos", str(games), "--wav",
                 "--sin-importar"]) == 2
