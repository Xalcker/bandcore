"""Pruebas del formato de notas común y del escritor de proyectos de FamiStudio."""

import re

from bandcore import famistudio, notes

DATA = notes.make(
    "Prueba", "Juego", 200,
    {
        "Square1": [[0, 30, 72, 13], [20, 10, 74, 9], [20, 5, 60, 9], [100, 150, 76, 15]],  # se enciman y una se pasa
        "Square2": [[0, 40, 64, 0]],  # muda
        "Triangle": [[0, 24, 40, 15], [24, 24, 43, 15], [48, 24, 45, 15], [72, 24, 40, 15]],
    },
    [[0, "kick", False], [0, "tom1", True], [24, "snare", False], [48, "tom2", False]],
)


def test_monophonic():
    assert DATA["channels"]["Square1"] == [[0, 20, 72, 13], [20, 10, 74, 9], [100, 150, 76, 15]]
    assert notes.monophonic([[5, 3, 60, 9], [0, 10, 62, 9]]) == [[0, 5, 62, 9], [5, 3, 60, 9]]


def test_project(tmp_path):
    path = tmp_path / "x.txt"
    info = famistudio.write_project(DATA, path)
    text = path.read_text(encoding="utf-8")
    assert info["frames_per_beat"] == 24 and info["beat"] * info["note_length"] == 24
    assert 'Copyright=""' not in text  # FamiStudio no abre el proyecto con el Copyright vacío
    assert 'Instrument Name="Guitarra 13"' in text and 'Instrument Name="Guitarra 9"' in text
    assert "Teclado" not in text  # la nota muda no crea instrumento
    # Una sola nota de ruido por cuadro: en el cuadro 0 gana el bombo al platillo
    noise = text.split('Channel Type="Noise"')[1].split('Channel Type="DPCM"')[0]
    assert 'Note Time="0" Value="A#2"' in noise and 'Value="G#3" Duration="1" Instrument="Platillo"' not in noise
    # La nota que se pasa del final se recorta a la duración de la canción
    durations = [int(d) for d in re.findall(r'Duration="(\d+)"', text.split('Channel Type="Square2"')[0])]
    assert max(durations) <= 99
    for kind in ("Square1", "Square2", "Triangle", "Noise", "DPCM"):
        assert f'Channel Type="{kind}"' in text
