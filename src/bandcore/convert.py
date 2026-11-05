"""Conversión directa de una canción a cualquiera de los cuatro juegos: `band convert`.

Encadena lo que hoy son varios comandos en repos distintos. Cada paso corre en el repo del juego que lo sabe hacer
(los clones de al lado: ../NESBand, ../GBBand, ../GENBand, ../NGBand), así que aquí no se repite código de los juegos:

    mismo chip (nativo)        <destino> import-song fuente
    otro chip (arreglo)        1. sacar las notas: <origen> export-notes (o band export-notes, para el YM2151)
                               2. NES, GB, NG: band famistudio -> proyecto de FamiStudio
                                  NG además: ngband_tools.famistudio -> VGM del YM2610
                                  GEN: audio/arreglo_fm.py (las notas a un VGM con sonidos FM)
                               3. <destino> import-song (GEN: arreglo_fm.py --importar)

Fuentes: VGM/VGZ (el chip decide: `bandcore.detect`), NSF/NSFE, GBS y proyectos de FamiStudio (.txt, .fms, .ftm).
Los archivos intermedios quedan en la carpeta de trabajo para revisarlos o repetir solo el último paso.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import detect
from .vgm import read_vgm

TARGETS = {"nes": "NESBand", "gb": "GBBand", "gen": "GENBand", "ng": "NGBand"}
TOOLS = {"NESBand": "nesband", "GBBand": "gbband", "GENBand": "genband", "NGBand": "ngband"}
TOOLS_TO_GAME = {tool: game for game, tool in TOOLS.items()}
NOTES_SECONDS = "@notas"  # en un comando: se cambia por la duración de notas.json justo antes de correrlo
MODULES = {"NESBand": "nesband_tools", "GBBand": "gbband_tools", "GENBand": "genband_tools", "NGBand": "ngband_tools"}
INSTRUMENTS = ("guitarra", "teclado", "bajo", "bateria")
# Qué opciones entiende cada comando (para no pasarle a un juego una que no tiene)
EXPORT_OPTIONS = {
    "band": {"title", "game", "duration", *INSTRUMENTS, "drums"},
    "genband": {"title", "game", "duration", *INSTRUMENTS},
    "ngband": {"title", "game", "duration", *INSTRUMENTS},
    "gbband": {"title", "game", "duration", "track", "drums"},
}
IMPORT_OPTIONS = {
    "nesband": {"title", "game", "duration", "track"},
    "gbband": {"title", "game", "duration", "track"},
    "genband": {"title", "game", "duration", *INSTRUMENTS},
    "ngband": {"title", "game", "duration", *INSTRUMENTS},
}
# Comandos cuyo --duration es entero
INT_DURATION = {("export", "genband"), ("export", "gbband"), ("import", "gbband"), ("import", "nesband")}
FAMISTUDIO_SUFFIXES = (".txt", ".fms", ".ftm")
# Fuentes que no son VGM: quién las importa tal cual y quién saca sus notas
NATIVE_FILES = {".nsf": ("NESBand", "GBBand"), ".nsfe": ("NESBand", "GBBand"), ".gbs": ("GBBand",),
                ".txt": ("NESBand", "GBBand"), ".fms": ("NESBand",), ".ftm": ("NESBand",)}
NAME = re.compile(r"[a-z][a-z0-9_]*")


@dataclass
class Step:
    """Un paso: un comando en el repo de un juego (`cwd`) o una función de bandcore (`fn`)."""

    description: str
    cmd: list[str] | None = None
    cwd: Path | None = None
    fn: Callable[[], None] | None = None
    outputs: list[Path] = field(default_factory=list)  # lo que debe existir al terminar

    def show(self) -> str:
        if self.cmd:
            return f"{self.description}\n    (en {self.cwd}) {' '.join(_quote(c) for c in self.cmd)}"
        return self.description


def _quote(text: str) -> str:
    return f'"{text}"' if " " in text or "(" in text else text


@dataclass
class Plan:
    source: Path
    target: str  # "NESBand"...
    mode: str  # detect.NATIVE o detect.ARRANGED
    origin: str  # de dónde salen las notas o quién importa ("genband export-notes", "nesband import-song"...)
    steps: list[Step]
    song_dir: Path | None  # la carpeta que crea el import (None con --sin-importar)
    work: Path


def default_games_dir() -> Path:
    """Donde están los clones de los juegos: $BAND_JUEGOS, o la carpeta que contiene a bandcore."""
    env = os.environ.get("BAND_JUEGOS")
    return Path(env) if env else Path(__file__).resolve().parents[3]


def _options(tool: str, accepted: dict[str, set[str]], opts: dict) -> list[str]:
    out = []
    for key, value in opts.items():
        if value is None or value is False or key not in accepted[tool]:
            continue
        if key == "duration" and (("export" if accepted is EXPORT_OPTIONS else "import"), tool) in INT_DURATION:
            value = int(round(float(value)))
        out += [f"--{key}", str(value)]
    return out


def _exporter(source: Path, chips: set[str]) -> str | None:
    """Quién saca las notas de la fuente."""
    suffix = source.suffix.lower()
    if suffix in (".nsf", ".nsfe", ".gbs", *FAMISTUDIO_SUFFIXES):
        return "gbband"
    if "ym2151" in chips:
        return "band"
    if chips & {"ym2612", "sn76489"}:
        return "genband"
    if "ym2610" in chips:
        return "ngband"
    if chips & {"gb_dmg", "nes_apu"}:
        return "gbband"
    return None


def _native(source: Path, chips: set[str]) -> tuple[str, ...]:
    """Los juegos que importan la fuente tal cual (el NES a la GB también: gbband import-song lo arregla solo)."""
    suffix = source.suffix.lower()
    if suffix in NATIVE_FILES:
        return NATIVE_FILES[suffix]
    games: list[str] = []
    if chips & {"ym2612", "sn76489"} and "ym2151" not in chips:
        games.append("GENBand")
    if "ym2610" in chips:
        games.append("NGBand")
    if "gb_dmg" in chips:
        games.append("GBBand")
    if "nes_apu" in chips:
        games += ["NESBand", "GBBand"]
    return tuple(games)


def make_plan(source: Path, target: str, name: str, games_dir: Path, work: Path, opts: dict, timbre: str = "A",
              import_song: bool = True, replace: bool = False) -> Plan:
    """Los pasos para llevar `source` al juego `target` ("nes", "gb", "gen" o "ng") con el nombre `name`.

    `opts`: title, game, duration, track, guitarra, teclado, bajo, bateria, drums (los que no vengan, None)."""
    if target not in TARGETS:
        raise ValueError(f"destino desconocido: {target} (son {', '.join(TARGETS)})")
    if not NAME.fullmatch(name):
        raise ValueError(f"nombre inválido: {name} (minúsculas, números y _; empieza con letra)")
    source = source.resolve()
    if not source.exists():
        raise ValueError(f"no existe: {source}")
    game = TARGETS[target]
    repo = games_dir / game
    chips: set[str] = set()
    if source.suffix.lower() in (".vgm", ".vgz"):
        chips = set(read_vgm(source).chips())
    elif source.suffix.lower() not in NATIVE_FILES:
        raise ValueError(f"{source.name}: formato que no conozco (VGM, VGZ, NSF, NSFE, GBS, FamiStudio .txt/.fms/.ftm)")
    work = work.resolve()
    tool = TOOLS[game]
    song_dir = repo / "src" / "songs" / name
    steps: list[Step] = []

    def need_repo(g: str) -> Path:
        path = games_dir / g
        if not (path / "pyproject.toml").exists():
            raise ValueError(f"no encontré {g} en {path} (clónalo junto a bandcore o usa --juegos)")
        return path

    if import_song or game in ("GENBand", "NGBand"):  # el arreglo de GEN y el paso al YM2610 corren en su repo
        need_repo(game)
    if import_song and song_dir.exists() and not replace:
        raise ValueError(f"{game} ya tiene una canción {name} ({song_dir}): usa otro --name o --reemplazar")

    if game in _native(source, chips):
        if not import_song:
            raise ValueError(f"{source.name} se importa tal cual en {game}: sin importar no hay nada que convertir")
        steps.append(Step(f"{game} la importa tal cual", ["uv", "run", tool, "import-song", str(source), "--name", name,
                                                          *_options(tool, IMPORT_OPTIONS, opts)], repo))
        return Plan(source, game, detect.NATIVE, f"{tool} import-song", steps, song_dir, work)

    exporter = _exporter(source, chips)
    if exporter is None:
        raise ValueError(f"{source.name}: nadie saca las notas de {', '.join(sorted(chips)) or 'este archivo'}")
    notes_json = work / "notas.json"
    if exporter == "band":
        def export_band() -> None:
            from .cli import main
            args = ["export-notes", str(source), "--out", str(notes_json), *_options("band", EXPORT_OPTIONS, opts)]
            if main(args) != 0:
                raise RuntimeError("band export-notes falló")

        steps.append(Step("bandcore saca las notas (band export-notes)", fn=export_band, outputs=[notes_json]))
    else:
        exporter_repo = need_repo(TOOLS_TO_GAME[exporter])
        steps.append(Step(f"{TOOLS_TO_GAME[exporter]} saca las notas",
                          ["uv", "run", exporter, "export-notes", str(source), "--out", str(notes_json),
                           *_options(exporter, EXPORT_OPTIONS, opts)], exporter_repo, outputs=[notes_json]))

    if game == "GENBand":
        cmd = ["uv", "run", "python", "audio/arreglo_fm.py", str(notes_json), "--nombre", name, "--timbre", timbre]
        arranged = repo / "songs_src" / "arreglos" / f"{name}.vgm"
        steps.append(Step("GENBand la arregla en FM" + (" y la importa" if import_song else ""),
                          cmd + (["--importar"] if import_song else []), repo, outputs=[arranged]))
        if not import_song:
            copy = work / f"{name}.vgm"
            steps.append(Step(f"copia el VGM arreglado a {copy}", fn=lambda: shutil.copyfile(arranged, copy),
                              outputs=[copy]))
        return Plan(source, game, detect.ARRANGED, exporter_tool(exporter), steps, song_dir if import_song else None,
                    work)

    project = work / "arreglo.txt"

    def famistudio() -> None:
        from .cli import main
        if main(["famistudio", str(notes_json), "--out", str(project), "--timbre", timbre]) != 0:
            raise RuntimeError("band famistudio falló")

    steps.append(Step(f"bandcore lo arregla como proyecto de FamiStudio (timbre {timbre})", fn=famistudio,
                      outputs=[project]))
    song_file = project
    if game == "NGBand":
        song_file = work / "arreglo_ng.vgm"
        steps.append(Step("NGBand lo pasa al YM2610",
                          ["uv", "run", "python", "-m", "ngband_tools.famistudio", str(project), str(song_file)],
                          repo, outputs=[song_file]))
    if import_song:
        import_opts = {k: v for k, v in opts.items() if k in ("title", "game")}
        if game == "NGBand":
            # El VGM convertido trae bucle: sin duración, ngband lo llenaría hasta 120 s
            import_opts["duration"] = opts.get("duration") or NOTES_SECONDS
        steps.append(Step(f"{game} la importa", ["uv", "run", tool, "import-song", str(song_file), "--name", name,
                                                 *_options(tool, IMPORT_OPTIONS, import_opts)], repo))
    return Plan(source, game, detect.ARRANGED, exporter_tool(exporter), steps, song_dir if import_song else None, work)


def exporter_tool(exporter: str) -> str:
    return f"{exporter} export-notes"


def _resolve_markers(step: Step, work: Path) -> None:
    if step.cmd and NOTES_SECONDS in step.cmd:
        import json
        frames = json.loads((work / "notas.json").read_text(encoding="utf-8"))["frames"]
        step.cmd[step.cmd.index(NOTES_SECONDS)] = f"{frames / 60 + 0.5:.1f}"


Runner = Callable[[list[str], Path], int]


def run_command(cmd: list[str], cwd: Path) -> int:
    # Sin el entorno virtual de bandcore: `uv run` en el repo del juego usa el suyo
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    return subprocess.run(cmd, cwd=cwd, env=env).returncode


def execute(plan: Plan, runner: Runner = run_command, echo: Callable[[str], None] = print) -> None:
    plan.work.mkdir(parents=True, exist_ok=True)
    for number, step in enumerate(plan.steps, 1):
        _resolve_markers(step, plan.work)
        echo(f"[{number}/{len(plan.steps)}] {step.show()}")
        if step.fn:
            step.fn()
        elif runner(step.cmd, step.cwd) != 0:
            raise RuntimeError(f"falló el paso {number}: {' '.join(step.cmd)}")
        missing = [str(p) for p in step.outputs if not p.exists()]
        if missing:
            raise RuntimeError(f"el paso {number} no dejó {', '.join(missing)}")


def wav_command(game: str, name: str, wav: Path) -> list[str]:
    """El comando (en el repo del juego) que renderiza la canción con el reproductor de su editor."""
    return ["uv", "run", "python", "-m", "bandcore.player_wav", MODULES[game], name, str(wav)]
