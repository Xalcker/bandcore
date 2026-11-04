"""Corredor de pruebas compartido por NESBand, GBBand, GENBand y NGBand.

Cada juego arma su lista de casos (``Case``) y su revisor, y este módulo se encarga de lo repetido: compilar,
lanzar el emulador (MAME o Mesen) sin ventana, correr los casos en paralelo, esperar con límite de tiempo,
resumir y decidir el código de salida. Todo en Python, así que funciona igual en macOS, Linux y Windows.

Uso típico, en el ``cli.py`` de un juego::

    p = sub.add_parser("test", help="corre las pruebas del juego")
    testrun.add_arguments(p)
    p.set_defaults(func=cmd_test)

    def cmd_test(args):
        return testrun.main(args, root=ROOT, cases=build_cases(args, ROOT), emulator=testrun.Mame(...),
                            build=testrun.build_command(ROOT))

Convenciones del resultado de cada caso: ``ok``, ``FALLA`` o ``salta``. Un ``FALLA`` que aparece en
``tests/known_failures.txt`` se cuenta como *conocido* y no cambia el código de salida; uno que no aparece es
*nuevo* y sí lo cambia. Un conocido que ahora pasa se avisa como *arreglado* para que lo quites de la lista.
"""
from __future__ import annotations

import argparse
import fnmatch
import math
import os
import platform
import shutil
import struct
import subprocess
import sys
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

OK, FAIL, SKIP = "ok", "FALLA", "salta"
KNOWN_FILE = Path("tests") / "known_failures.txt"


# ---------------------------------------------------------------------------------------------------
# Casos y resultados
# ---------------------------------------------------------------------------------------------------

@dataclass
class Result:
    status: str = OK
    summary: str = ""
    problems: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.status == FAIL


@dataclass
class Case:
    """Un caso: un arranque del emulador con las variables de entorno que lee el bot (``autoplay.lua``)."""
    name: str
    env: dict[str, str] = field(default_factory=dict)
    out: Path | None = None                 # carpeta de salida; por omisión build/test-<name>
    check: Callable[["Case", Path, int], Result] | None = None   # (caso, carpeta, código de salida) -> Result
    serial: bool = False                    # no corre en paralelo; va después, en el orden de la lista
    nvram: Path | None = None               # carpeta de nvram/SRAM compartida entre casos (récords)
    fresh_nvram: bool = False               # borra nvram antes de correr (el primer caso de récords)
    skip: str | None = None                 # motivo por el que no puede correr (falta una canción, etc.)
    skip_is_failure: bool = False           # si faltar es una falla (la canción de demostración) y no un "salta"
    extra: dict = field(default_factory=dict)   # lo que el emulador o el revisor del juego necesiten


class Emulator(Protocol):
    def prepare(self, root: Path, slots: int) -> None: ...
    def command(self, case: Case, slot: int) -> tuple[list[str], dict[str, str]]: ...
    def run(self, case: Case, slot: int, log: Path, timeout: float) -> int: ...


# ---------------------------------------------------------------------------------------------------
# Lectores comunes de lo que deja el bot
# ---------------------------------------------------------------------------------------------------

def read_result(folder: Path) -> tuple[dict[str, str], dict[str, int]]:
    """``resultado.txt`` (clave=valor): el texto de cada clave y los enteros aparte."""
    r: dict[str, str] = {}
    with open(folder / "resultado.txt", encoding="utf-8") as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                r[k] = v
    return r, {k: int(v) for k, v in r.items() if v.lstrip("-").isdigit()}


def wav_rms(path: Path) -> float:
    """RMS del WAV de 16 bits que graba MAME (se toma una de cada 97 muestras: basta para saber si sonó)."""
    with wave.open(str(path)) as w:
        data = w.readframes(w.getnframes())
    s = struct.unpack(f"<{len(data) // 2}h", data[: len(data) // 2 * 2])[::97]
    return math.sqrt(sum(x * x for x in s) / max(1, len(s)))


# ---------------------------------------------------------------------------------------------------
# Emuladores
# ---------------------------------------------------------------------------------------------------

def find_exe(env_var: str, names: Sequence[str], extra_dirs: Iterable[Path] = ()) -> str | None:
    """El ejecutable: la variable de entorno, el PATH o carpetas conocidas (en Windows, ``C:/Program Files``)."""
    if os.environ.get(env_var):
        return os.environ[env_var]
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    for d in extra_dirs:
        for n in names:
            for suffix in ("", ".exe"):
                p = Path(d) / (n + suffix)
                if p.is_file():
                    return str(p)
    return None


def _terminate(proc: subprocess.Popen) -> None:
    proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _run_process(argv: list[str], env: dict[str, str], log: Path, timeout: float,
                 cwd: Path | None = None) -> int:
    """Corre un proceso con su salida en ``log``; si pasa del límite lo mata y devuelve -1."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "wb") as out:
        proc = subprocess.Popen(argv, env=env, cwd=cwd, stdout=out, stderr=subprocess.STDOUT)
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate(proc)
            out.write(f"\n[testrun] tiempo agotado ({timeout:.0f} s)\n".encode())
            return -1


class Mame:
    """MAME en ventana (``-window -nomaximize``, nunca en pantalla completa) y sin esperar al reloj (``-nothrottle``).

    ``machine_args(case)`` da lo propio del juego: ``["genesis", "-cart", rom]`` o ``["neogeo", "-hash", ...]``.
    En macOS la ventana (SDL) toma el foco al aparecer y se le devuelve a la aplicación que estaba al frente.
    Con ``window=False`` corre sin video (``-video none``).
    """

    def __init__(self, machine_args: Callable[[Case], list[str]], script: Path, window: bool = False,
                 resolution: str | None = None, env: Callable[[Case], dict[str, str]] | None = None):
        """``env(case)``: variables de entorno extra por caso (además de ``case.env``), por ejemplo las que lee un
        script de compatibilidad."""
        self.machine_args, self.script, self.window, self.resolution = machine_args, script, window, resolution
        self.extra_env = env
        self.exe = find_exe("MAME", ["mame"], [Path("C:/Program Files/MAME"), Path("C:/MAME"), Path("C:/mame")])

    def prepare(self, root: Path, slots: int) -> None:
        if not self.exe:
            raise SystemExit("Falta MAME: brew install mame (macOS), o MAME en el PATH / variable MAME (Windows)")

    def command(self, case: Case, slot: int) -> tuple[list[str], dict[str, str]]:
        out = case.out
        nvram = case.nvram or out / "nvram"
        argv = [self.exe, *self.machine_args(case), "-sound", "none", "-nothrottle", "-skip_gameinfo",
                "-frameskip", "0", "-noautoframeskip", "-wavwrite", str(out / "audio.wav"),
                "-snapshot_directory", str(out), "-cfg_directory", str(out / "cfg"),
                "-nvram_directory", str(nvram), "-autoboot_script", str(self.script)]
        env = {**os.environ, **case.env, **(self.extra_env(case) if self.extra_env else {})}
        if self.window:
            argv += ["-window", "-nomaximize"] + (["-resolution", self.resolution] if self.resolution else [])
        else:
            # Ojo: en macOS, ``-video none`` por sí solo sigue abriendo una ventana maximizada y al frente (por la
            # configuración por omisión de MAME). El driver "dummy" de SDL no crea ninguna.
            argv += ["-video", "none"]
            env.setdefault("SDL_VIDEODRIVER", "dummy")
        return argv, env

    def run(self, case: Case, slot: int, log: Path, timeout: float) -> int:
        argv, env = self.command(case, slot)
        if not (self.window and platform.system() == "Darwin"):
            return _run_process(argv, env, log, timeout)
        return _run_window_mac(argv, env, log, timeout)


def _lsappinfo(*args: str) -> str:
    try:
        return subprocess.run(["lsappinfo", *args], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _run_window_mac(argv, env, log: Path, timeout: float) -> int:
    """MAME con ventana en macOS: la ventana (SDL) toma el foco al aparecer; se lo devolvemos a quien lo tenía."""
    front = _lsappinfo("front").strip()
    bundle = ""
    if front:
        import re
        m = re.search(r'"CFBundleIdentifier"="([^"]*)"', _lsappinfo("info", "-only", "bundleid", front))
        bundle = m.group(1) if m else ""
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "wb") as out:
        proc = subprocess.Popen(argv, env=env, stdout=out, stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        if bundle:
            for _ in range(50):                     # hasta 5 s a que cambie la app al frente
                time.sleep(0.1)
                if proc.poll() is not None:
                    break
                now = _lsappinfo("front").strip()
                if now and now != front:
                    subprocess.run(["open", "-b", bundle], check=False)
                    break
        try:
            return proc.wait(timeout=max(1.0, deadline - time.time()))
        except subprocess.TimeoutExpired:
            _terminate(proc)
            return -1


class Mesen:
    """Mesen 2 en ``--testrunner`` (sin ventana). Cada ranura paralela usa su copia del ROM, así que los .sav
    no se mezclan. El caso pasa o falla por el código de salida del script Lua, salvo que traiga su ``check``."""

    def __init__(self, rom: Path, script: Path, settings_json: str, *, mesen_dir: Path | None = None):
        self.rom, self.script, self.settings_json = rom, script, settings_json
        self.mesen_dir = mesen_dir
        self.exe: Path | None = None
        self.par_dir: Path | None = None

    def locate(self, root: Path) -> Path:
        base = self.mesen_dir or root / "tools" / "mesen"
        system = platform.system()
        rel = {"Darwin": "Mesen.app/Contents/MacOS/Mesen", "Linux": "Mesen"}.get(system, "Mesen.exe")
        exe = base / rel
        if not exe.is_file():
            raise SystemExit(f"No encontré {exe.relative_to(root) if exe.is_relative_to(root) else exe} (ver README)")
        return exe

    def prepare(self, root: Path, slots: int) -> None:
        self.exe = self.locate(root)
        settings = self.exe.parent / "settings.json"
        if not settings.exists():
            # un settings.json junto al ejecutable pone a Mesen en modo portable (no toca la configuración normal)
            settings.write_text(self.settings_json, encoding="utf-8")
            print(f"Creé {settings} (Mesen portable para las pruebas)")
        self.par_dir = root / "build" / "par"
        self.par_dir.mkdir(parents=True, exist_ok=True)
        self.saves = self.exe.parent / "Saves"
        for i in range(max(1, slots)):
            shutil.copy2(self.rom, self.par_dir / f"par{i}{self.rom.suffix}")
            dbg = self.rom.with_suffix(".dbg")
            if dbg.exists():
                shutil.copy2(dbg, self.par_dir / f"par{i}.dbg")

    def rom_for(self, case: Case, slot: int) -> Path:
        return self.rom if case.serial else self.par_dir / f"par{slot}{self.rom.suffix}"

    def command(self, case: Case, slot: int) -> tuple[list[str], dict[str, str]]:
        argv = [str(self.exe), "--testrunner", str(self.rom_for(case, slot).resolve()), str(self.script.resolve())]
        return argv, {**os.environ, **case.env, "TEST_OUT": str(case.out)}

    def run(self, case: Case, slot: int, log: Path, timeout: float) -> int:
        # cada caso empieza con los récords vacíos, salvo los que continúan los de otro (fresh_nvram == False y serial)
        sav = self.saves / (self.rom_for(case, slot).stem + ".sav")
        if not case.serial or case.fresh_nvram:
            sav.unlink(missing_ok=True)
        (case.out / "resultado.txt").unlink(missing_ok=True)
        argv, env = self.command(case, slot)
        return _run_process(argv, env, log, timeout)


# ---------------------------------------------------------------------------------------------------
# Compilar
# ---------------------------------------------------------------------------------------------------

def build_command(root: Path, release: bool = False) -> list[str] | None:
    """El comando de compilación del repo para este sistema: ``build.ps1`` en Windows (si existe), si no ``build.sh``."""
    ps1, sh = root / "build.ps1", root / "build.sh"
    flag = ["-Release"] if release else []
    if platform.system() == "Windows":
        if ps1.exists():
            shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
            return [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), *flag]
        return None
    if sh.exists():
        return ["bash", str(sh), *(["--release"] if release else [])]
    if ps1.exists() and shutil.which("pwsh"):
        return [shutil.which("pwsh"), "-NoProfile", "-File", str(ps1), *flag]
    return None


# ---------------------------------------------------------------------------------------------------
# Corredor
# ---------------------------------------------------------------------------------------------------

def add_arguments(p: argparse.ArgumentParser, *, rapida: bool = True, jobs: bool = True) -> None:
    """Las opciones que todos los juegos aceptan en ``<juego> test``."""
    p.add_argument("patterns", nargs="*", metavar="patron",
                   help="solo los casos cuyo nombre coincide (comodines y comas: 'tormenta-*,demo-*')")
    p.add_argument("--no-build", action="store_true", help="no compila: usa la ROM que ya está en build/")
    if rapida:
        p.add_argument("--rapida", action="store_true", help="un subconjunto representativo (cambios del motor)")
    if jobs:
        p.add_argument("-j", "--paralelo", type=int, default=1, metavar="N", help="casos a la vez (por omisión 1)")
    p.add_argument("--sin-ventana", dest="sin_ventana", action="store_true",
                   help="MAME invisible, sin ninguna ventana (no te interrumpe); por omisión abre en una ventana "
                        "chica, nunca en pantalla completa")
    p.add_argument("--lista", action="store_true", help="solo lista los casos que correrían")
    p.add_argument("--timeout", type=float, default=1800, metavar="SEG", help="límite por caso (1800 s)")


def split_patterns(patterns: Sequence[str]) -> list[str]:
    out: list[str] = []
    for p in patterns:
        out += [x for x in p.replace(",", " ").split() if x]
    return out


def matches(name: str, patterns: Sequence[str]) -> bool:
    return not patterns or any(fnmatch.fnmatchcase(name, p) for p in patterns)


def load_known(root: Path) -> set[str]:
    f = root / KNOWN_FILE
    if not f.exists():
        return set()
    return {ln.split("#", 1)[0].strip() for ln in f.read_text(encoding="utf-8").splitlines()
            if ln.split("#", 1)[0].strip()}


def _execute(case: Case, emulator: Emulator, slot: int, timeout: float) -> Result:
    if case.skip:
        return Result(FAIL, case.skip) if case.skip_is_failure else Result(SKIP, case.skip)
    out = case.out
    if case.fresh_nvram and case.nvram:
        shutil.rmtree(case.nvram, ignore_errors=True)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)
    code = emulator.run(case, slot, out / "emulador.log", timeout)
    if code == -1:
        return Result(FAIL, "tiempo agotado", [f"pasó de {timeout:.0f} s (ver {out / 'emulador.log'})"])
    if case.check is None:
        return Result(OK if code == 0 else FAIL, f"código de salida {code}")
    try:
        return case.check(case, out, code)
    except FileNotFoundError:
        return Result(FAIL, "el bot no llegó a los resultados", [f"revisa {out}"])
    except Exception as e:                                  # un revisor roto es una falla del caso, no del corredor
        return Result(FAIL, f"el revisor falló: {type(e).__name__}: {e}")


def run_cases(cases: list[Case], emulator: Emulator, *, jobs: int = 1, timeout: float = 1800,
              known: set[str] = frozenset(), echo: Callable[[str], None] = print) -> dict[str, Result]:
    """Corre los casos. Los ``serial`` van al final, de uno en uno y en orden; los demás, de ``jobs`` en ``jobs``."""
    lock = threading.Lock()
    results: dict[str, Result] = {}

    def report(case: Case, r: Result) -> None:
        mark = r.status if r.status != FAIL or case.name not in known else "FALLA (conocida)"
        text = f"{mark:<5} {case.name}: {r.summary}" + "".join(f"\n      - {p}" for p in r.problems)
        with lock:
            results[case.name] = r
            echo(text)

    parallel = [c for c in cases if not c.serial]
    serial = [c for c in cases if c.serial]
    slots: list[int] = list(range(max(1, jobs)))
    free_lock = threading.Lock()

    def work(c: Case) -> None:
        with free_lock:
            slot = slots.pop() if slots else 0
        try:
            report(c, _execute(c, emulator, slot, timeout))
        finally:
            with free_lock:
                slots.append(slot)

    if parallel:
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
            list(ex.map(work, parallel))
    for c in serial:
        report(c, _execute(c, emulator, 0, timeout))
    return results


def summarize(results: dict[str, Result], known: set[str], echo: Callable[[str], None] = print) -> int:
    """Imprime el resumen y devuelve el código de salida: 1 solo si hay fallas que no estaban en la lista."""
    failed = {n for n, r in results.items() if r.failed}
    new = sorted(failed - known)
    old = sorted(failed & known)
    fixed = sorted(n for n in known if n in results and not results[n].failed and results[n].status == OK)
    skipped = [n for n, r in results.items() if r.status == SKIP]
    total_ok = sum(1 for r in results.values() if r.status == OK)
    echo("")
    echo(f"{total_ok} ok, {len(failed)} con falla ({len(old)} conocidas, {len(new)} nuevas), {len(skipped)} saltados "
         f"de {len(results)} casos")
    if fixed:
        echo(f"Arreglados (quítalos de {KNOWN_FILE.as_posix()}): {', '.join(fixed)}")
    if new:
        echo("FALLAS NUEVAS: " + ", ".join(new))
        return 1
    echo("Todo bien" if not old else "Sin fallas nuevas")
    return 0


def main(args: argparse.Namespace, *, root: Path, cases: list[Case], emulator: Emulator,
         build: list[str] | None = None, build_hint: str = "",
         post: Callable[[Path, dict[str, Result]], dict[str, Result]] | None = None) -> int:
    """El ``<juego> test`` completo. ``cases`` ya viene armado por el juego (con ``args`` a la vista).

    ``post(root, resultados)`` es opcional: revisa los casos en conjunto (por ejemplo, la tasa de partidas del bot
    humano) y devuelve resultados extra con su nombre, que se imprimen y cuentan como un caso más."""
    patterns = split_patterns(args.patterns)
    selected = [c for c in cases if matches(c.name, patterns)]
    if args.lista:
        for c in selected:
            print(c.name + (f"   (salta: {c.skip})" if c.skip else ""))
        return 0
    if not selected:
        print(f"Ningún caso coincide con {' '.join(patterns)!r}", file=sys.stderr)
        return 1
    if not args.no_build:
        if build is None:
            print("Aquí no se puede compilar con este sistema" + (f": {build_hint}" if build_hint else "")
                  + ". Compila donde sí se pueda y corre con --no-build.", file=sys.stderr)
            return 1
        if subprocess.run(build, cwd=root).returncode != 0:
            return 1
    emulator.prepare(root, args.paralelo)
    for c in selected:
        if c.out is None:
            c.out = root / "build" / f"test-{c.name}"
    known = load_known(root)
    results = run_cases(selected, emulator, jobs=args.paralelo, timeout=args.timeout, known=known)
    if post:
        for name, r in post(root, results).items():
            results[name] = r
            mark = r.status if r.status != FAIL or name not in known else "FALLA (conocida)"
            print(f"{mark:<5} {name}: {r.summary}" + "".join(f"\n      - {p}" for p in r.problems))
    return summarize(results, known)
