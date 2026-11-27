"""El chart compartido (charts.json): ida y vuelta, la entrada de la autopista de cada juego y la copia a los demás."""

from __future__ import annotations

import json

import pytest

from bandcore import sharedcharts
from bandcore.chart import ChartNote


def sample() -> dict[str, list[ChartNote]]:
    return {
        "guitarra/facil": [ChartNote(128, 1), ChartNote(168, 6, 200, True)],
        "bateria/experto": [ChartNote(130, 0b10001), ChartNote(150, 0b00100, None, False, True)],
    }


def test_ida_y_vuelta_con_otra_entrada(tmp_path):
    sharedcharts.save(tmp_path, sample(), 128, "NESBand", 4480, 20, 0)
    data = json.loads((tmp_path / sharedcharts.CHARTS_FILE).read_text(encoding="utf-8"))
    assert data["charts"]["guitarra/facil"][1] == [40, 6, 72, 1, 0]   # cuadros desde que arranca la música
    loaded = sharedcharts.load(tmp_path, 180)                           # la Game Boy tiene otra entrada
    assert loaded.chart("guitarra", "facil") == [ChartNote(180, 1), ChartNote(220, 6, 252, True)]
    assert loaded.chart("bateria", "experto")[1].cymbal
    assert loaded.instruments() == ["guitarra", "bateria"]
    assert (loaded.music_frames, loaded.frames_per_beat, loaded.first_beat) == (4480, 20, 0)


def test_sin_archivo_no_hay_chart(tmp_path):
    assert sharedcharts.load(tmp_path, 128) is None


def test_version_desconocida(tmp_path):
    (tmp_path / sharedcharts.CHARTS_FILE).write_text('{"version": 9}', encoding="utf-8")
    with pytest.raises(ValueError):
        sharedcharts.load(tmp_path, 128)


def test_sync_copia_a_la_misma_cancion(tmp_path):
    nes, gb, gen = tmp_path / "NESBand", tmp_path / "GBBand", tmp_path / "GENBand"
    for root, song in ((nes, "tormenta"), (nes, "nesband_demo"), (gb, "tormenta"), (gen, "demo")):
        (root / "src" / "songs" / song).mkdir(parents=True)
    sharedcharts.save(nes / "src/songs/tormenta", sample(), 128, "NESBand", 4480, 20, 0)
    sharedcharts.save(nes / "src/songs/nesband_demo", sample(), 128, "NESBand", 3072, 24, 0)
    done = sharedcharts.sync(nes, [gb, gen])
    assert sorted(done) == ["GBBand/tormenta", "GENBand/demo"]   # nesband_demo se llama demo; GEN no tiene tormenta
    assert sharedcharts.sync(nes, [gb, gen]) == []               # ya está al día
