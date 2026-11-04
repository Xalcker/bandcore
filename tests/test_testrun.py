"""Pruebas del corredor compartido (bandcore.testrun) con un "emulador" falso hecho con Python:
sirve igual en macOS, Linux y Windows."""
import argparse
import sys
import wave
from pathlib import Path

import pytest

from bandcore import testrun
from bandcore.testrun import Case, Result


class FakeEmu:
    """Escribe resultado.txt con lo que diga la variable FAKE (o se cuelga con FAKE=hang)."""

    def __init__(self):
        self.slots_seen = []

    def prepare(self, root, slots):
        self.slots = slots

    def run(self, case, slot, log, timeout):
        self.slots_seen.append(slot)
        code = ("import os,sys,time\nmode=os.environ['FAKE']\n"
                "if mode=='hang': time.sleep(60)\n"
                "open(os.path.join(os.environ['OUT'],'resultado.txt'),'w').write('notas=3\\ntotal=3\\n')\n"
                "sys.exit(0 if mode=='ok' else 3)")
        env = {**__import__('os').environ, **case.env, "OUT": str(case.out)}
        return testrun._run_process([sys.executable, "-c", code], env, log, timeout)


def check(case, folder, code):
    r, n = testrun.read_result(folder)
    return Result(testrun.OK if n["notas"] == n["total"] and code == 0 else testrun.FAIL, f"notas {n['notas']}/{n['total']}")


def mk(tmp_path, name, mode, **kw):
    return Case(name, {"FAKE": mode}, out=tmp_path / f"test-{name}", check=check, **kw)


def test_ok_y_falla_y_codigo_de_salida(tmp_path):
    cases = [mk(tmp_path, "a", "ok"), mk(tmp_path, "b", "mal")]
    lines = []
    res = testrun.run_cases(cases, FakeEmu(), echo=lines.append)
    assert res["a"].status == "ok" and res["b"].status == "FALLA"
    assert testrun.summarize(res, set(), lambda s: None) == 1


def test_una_falla_conocida_no_cambia_el_codigo(tmp_path):
    res = testrun.run_cases([mk(tmp_path, "b", "mal")], FakeEmu(), known={"b"}, echo=lambda s: None)
    out = []
    assert testrun.summarize(res, {"b"}, out.append) == 0
    assert "1 conocidas, 0 nuevas" in " ".join(out)


def test_un_conocido_que_pasa_se_avisa(tmp_path):
    res = testrun.run_cases([mk(tmp_path, "a", "ok")], FakeEmu(), echo=lambda s: None)
    out = []
    testrun.summarize(res, {"a"}, out.append)
    assert any("Arreglados" in s and "a" in s for s in out)


def test_tiempo_agotado_es_falla(tmp_path):
    res = testrun.run_cases([mk(tmp_path, "h", "hang")], FakeEmu(), timeout=1, echo=lambda s: None)
    assert res["h"].failed and "tiempo agotado" in res["h"].summary


def test_los_serial_van_al_final_y_en_orden(tmp_path):
    cases = [mk(tmp_path, "r1", "ok", serial=True), mk(tmp_path, "p1", "ok"), mk(tmp_path, "r2", "ok", serial=True),
             mk(tmp_path, "p2", "ok")]
    lines = []
    testrun.run_cases(cases, FakeEmu(), jobs=2, echo=lines.append)
    order = [ln.split()[1].rstrip(":") for ln in lines if ln.startswith("ok")]
    assert order[-2:] == ["r1", "r2"] and set(order[:2]) == {"p1", "p2"}


def test_paralelo_usa_ranuras_distintas(tmp_path):
    emu = FakeEmu()
    testrun.run_cases([mk(tmp_path, f"c{i}", "ok") for i in range(6)], emu, jobs=3, echo=lambda s: None)
    assert set(emu.slots_seen) <= {0, 1, 2}


def test_caso_saltado(tmp_path):
    res = testrun.run_cases([Case("s", skip="falta la canción", out=tmp_path / "s")], FakeEmu(), echo=lambda s: None)
    assert res["s"].status == "salta"


def test_revisor_roto_es_falla_del_caso(tmp_path):
    def roto(case, folder, code):
        raise ValueError("boom")
    c = Case("x", {"FAKE": "ok"}, out=tmp_path / "x", check=roto)
    res = testrun.run_cases([c], FakeEmu(), echo=lambda s: None)
    assert res["x"].failed and "boom" in res["x"].summary


def test_patrones():
    assert testrun.split_patterns(["a-*,b", "c"]) == ["a-*", "b", "c"]
    assert testrun.matches("tormenta-nada", ["tormenta-*"]) and not testrun.matches("demo", ["tormenta-*"])
    assert testrun.matches("lo que sea", [])


def test_load_known_ignora_comentarios(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "known_failures.txt").write_text("# nota\na  # por qué\n\nb\n", encoding="utf-8")
    assert testrun.load_known(tmp_path) == {"a", "b"}


def test_main_lista_y_sin_coincidencias(tmp_path, capsys):
    p = argparse.ArgumentParser()
    testrun.add_arguments(p)
    cases = [mk(tmp_path, "a", "ok")]
    assert testrun.main(p.parse_args(["--lista", "--no-build"]), root=tmp_path, cases=cases, emulator=FakeEmu()) == 0
    assert "a" in capsys.readouterr().out
    assert testrun.main(p.parse_args(["zzz", "--no-build"]), root=tmp_path, cases=cases, emulator=FakeEmu()) == 1


def test_main_completo(tmp_path):
    p = argparse.ArgumentParser()
    testrun.add_arguments(p)
    cases = [mk(tmp_path, "a", "ok"), mk(tmp_path, "b", "mal")]
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "known_failures.txt").write_text("b\n", encoding="utf-8")
    assert testrun.main(p.parse_args(["--no-build"]), root=tmp_path, cases=cases, emulator=FakeEmu()) == 0


def test_main_sin_compilador_avisa(tmp_path, capsys):
    p = argparse.ArgumentParser()
    testrun.add_arguments(p)
    rc = testrun.main(p.parse_args([]), root=tmp_path, cases=[mk(tmp_path, "a", "ok")], emulator=FakeEmu(), build=None)
    assert rc == 1 and "--no-build" in capsys.readouterr().err


def test_read_result_y_wav_rms(tmp_path):
    (tmp_path / "resultado.txt").write_text("notas=5\niniciales=BOT\nfallo=-1\n", encoding="utf-8")
    r, n = testrun.read_result(tmp_path)
    assert r["iniciales"] == "BOT" and n == {"notas": 5, "fallo": -1}
    with wave.open(str(tmp_path / "a.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(b"".join((1000 if i % 2 else -1000).to_bytes(2, "little", signed=True) for i in range(5000)))
    assert testrun.wav_rms(tmp_path / "a.wav") == pytest.approx(1000, rel=0.01)


def test_build_command_segun_el_sistema(tmp_path, monkeypatch):
    (tmp_path / "build.sh").write_text("")
    monkeypatch.setattr(testrun.platform, "system", lambda: "Darwin")
    assert testrun.build_command(tmp_path)[0] == "bash"
    monkeypatch.setattr(testrun.platform, "system", lambda: "Windows")
    assert testrun.build_command(tmp_path) is None            # sin build.ps1 no hay cómo compilar en Windows
    (tmp_path / "build.ps1").write_text("")
    assert "-File" in testrun.build_command(tmp_path, release=True) and "-Release" in testrun.build_command(tmp_path, release=True)
