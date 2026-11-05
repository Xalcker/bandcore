"""Línea de comandos de bandcore.

    uv run band inspect cancion.vgz [otra.vgm ...] [--json]
    uv run band famistudio notas.json --out arreglo.txt [--timbre A|B|todos]
    uv run band channels cancion.vgz                           (YM2151: los canales y la batería sugerida)
    uv run band export-notes cancion.vgz --out notas.json [--guitarra fm1] [--drums drums.json] ...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import detect
from .vgm import Vgm, read_vgm


def _time(samples: int) -> str:
    seconds = samples / 44100
    return f"{int(seconds // 60)}:{seconds % 60:04.1f}"


def _describe(path: Path, vgm: Vgm, report: detect.Report) -> str:
    lines = [f"{path.name}  (VGM {vgm.version >> 8:x}.{vgm.version & 0xFF:02x})"]
    for key, label in (("title", "Título"), ("game", "Juego"), ("system", "Sistema"), ("author", "Autor")):
        if key in vgm.tags:
            lines.append(f"  {label + ':':9}{vgm.tags[key]}")
    loop = f", bucle desde {_time(vgm.loop_sample)}" if vgm.loop_sample is not None and vgm.loop_samples else ", sin bucle"
    lines.append(f"  Duración: {_time(vgm.total_samples)}{loop}")
    lines.append(f"  Consola:  {report.system}")
    used = vgm.used
    for chip in report.chips:
        clock = vgm.clocks.get(chip)
        hz = f", {clock / 1e6:.3f} MHz" if clock else ""
        lines.append(f"    - {detect.chip_name(chip, vgm)}: {used[chip]} escrituras{hz}")
    if vgm.blocks:
        kinds = sorted({b.type for b in vgm.blocks})
        size = sum(len(b.data) for b in vgm.blocks)
        lines.append(f"  Datos:    {size} bytes en bloques de tipo " + ", ".join(f"${k:02X}" for k in kinds))
    if vgm.other:
        cmds = sorted({o.cmd for o in vgm.other})
        lines.append("  Sin leer: comandos " + ", ".join(f"${c:02X}" for c in cmds))
    if report.lost:
        lines.append("  Se pierde: " + ", ".join(detect.chip_name(c, vgm) for c in report.lost))
    lines.append("  Destinos:")
    if not report.targets:
        lines.append("    ninguno")
    for t in report.targets:
        state = "listo" if t.ready else "pendiente"
        note = f" ({t.note})" if t.note else ""
        lines.append(f"    {t.game:8} {t.mode:8} {state}{note}")
    return "\n".join(lines)


def _as_dict(path: Path, vgm: Vgm, report: detect.Report) -> dict:
    return {
        "file": str(path),
        "version": vgm.version,
        "tags": vgm.tags,
        "seconds": round(vgm.seconds, 3),
        "loop_sample": vgm.loop_sample,
        "system": report.system,
        "chips": {c: {"writes": vgm.used[c], "clock": vgm.clocks.get(c)} for c in report.chips},
        "lost": report.lost,
        "targets": [t.__dict__ for t in report.targets],
    }


def cmd_inspect(args: argparse.Namespace) -> int:
    failed = 0
    results = []
    for name in args.files:
        path = Path(name)
        try:
            vgm = read_vgm(path)
        except (OSError, ValueError) as exc:
            print(f"{path}: {exc}", file=sys.stderr)
            failed += 1
            continue
        report = detect.analyze(vgm)
        if args.json:
            results.append(_as_dict(path, vgm, report))
        else:
            print(_describe(path, vgm, report))
            print()
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 1 if failed else 0


def cmd_famistudio(args: argparse.Namespace) -> int:
    """Las notas de `<juego> export-notes` a un proyecto de FamiStudio, con el timbre A, B o los dos."""
    from . import famistudio, notes

    data = notes.read(args.notes)
    out = Path(args.out)
    timbres = list(famistudio.TIMBRES) if args.timbre == "todos" else [args.timbre]
    for timbre in timbres:
        path = out if timbre == "A" else out.with_name(f"{out.stem}_{timbre.lower()}{out.suffix}")
        info = famistudio.write_project(data, path, timbre=timbre)
        sounds = ", ".join(s[0] for s in famistudio.TIMBRES[timbre].values())
        print(f"{path}: timbre {timbre} ({sounds}); {info['patterns']} compases de {info['frames_per_beat'] * 4} cuadros, "
              f"{info['instruments']} instrumentos")
    return 0


def _arcade_song(args: argparse.Namespace):
    from . import arcade
    from .drums import DRUMS_VERSION

    assignment = None
    drums_path = Path(args.drums) if getattr(args, "drums", None) else None
    if drums_path and drums_path.exists():
        data = json.loads(drums_path.read_text(encoding="utf-8"))
        if data.get("version") != DRUMS_VERSION:
            raise ValueError(f"{drums_path}: versión de drums.json no soportada ({data.get('version')})")
        assignment = data.get("groups", {})
    channels = {name: getattr(args, name, None) for name in (*arcade.INSTRUMENTS, "bateria")}
    return arcade.load(args.source, args.duration, getattr(args, "title", None), getattr(args, "game", None),
                       channels, assignment)


def cmd_channels(args: argparse.Namespace) -> int:
    """Los canales de una canción de YM2151, qué instrumento sería cada uno y los grupos de la batería."""
    from . import arcade

    try:
        song = _arcade_song(args)
    except (OSError, ValueError) as exc:
        print(f"band: {exc}", file=sys.stderr)
        return 1
    print("\n".join(arcade.describe(song)))
    return 0


def cmd_export_notes(args: argparse.Namespace) -> int:
    """Las notas de una canción de YM2151 en el formato de bandcore.notes. Con --drums, los papeles de la batería
    salen de ese drums.json; si todavía no existe, se escribe ahí la sugerencia para corregirla."""
    from . import arcade, notes
    from .drums import dump_drums

    try:
        song = _arcade_song(args)
    except (OSError, ValueError) as exc:
        print(f"band: {exc}", file=sys.stderr)
        return 1
    data = arcade.to_notes(song)
    notes.write(data, Path(args.out))
    if args.drums and not Path(args.drums).exists():
        Path(args.drums).write_text(dump_drums(song.assignment), encoding="utf-8", newline="\n")
        print(f"{args.drums}: la batería sugerida (corrígela y vuelve a exportar)")
    used = ", ".join(f"{name} {'+'.join(song.assign.get(name, [])) or '-'}" for name in arcade.INSTRUMENTS)
    print(f"{args.out}: {song.title} ({song.game}), {song.frames / 60:.1f} s; {used}; "
          f"batería {'+'.join(song.drums) or '-'} ({len(data['drums'])} golpes)")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Las etiquetas GD3 traen japonés: en UTF-8 aunque la salida vaya a una tubería de Windows (que usaría cp1252)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="band", description="Herramientas compartidas de NESBand, GBBand, GENBand y NGBand")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("inspect", help="dice qué chips usa un VGM y para qué juego sirve")
    p.add_argument("files", nargs="+")
    p.add_argument("--json", action="store_true", help="salida en JSON")
    p.set_defaults(func=cmd_inspect)
    p = sub.add_parser("famistudio", help="arregla las notas de otra consola (de <juego> export-notes) como proyecto de "
                                          "FamiStudio para NESBand, GBBand o NGBand")
    p.add_argument("notes", help="JSON de export-notes")
    p.add_argument("--out", required=True, help="proyecto de salida (.txt); el timbre B sale como <nombre>_b.txt")
    p.add_argument("--timbre", default="A", choices=["A", "B", "todos"],
                   help="A: guitarra, piano y bajo (por omisión); B: sintetizador, pulsada y bajo")
    p.set_defaults(func=cmd_famistudio)
    for name, helptext in (("channels", "muestra los canales de un VGM de YM2151 (arcade) y la batería sugerida"),
                           ("export-notes", "saca las notas de un VGM de YM2151 (arcade) a JSON (bandcore.notes) para "
                                            "arreglarlas en otra consola")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("source")
        if name == "export-notes":
            p.add_argument("--out", required=True)
            p.add_argument("--title")
            p.add_argument("--game")
        p.add_argument("--duration", type=float, help="segundos que se toman (por omisión, el archivo; con bucle, al "
                                                      "menos 120)")
        for inst in ("guitarra", "teclado", "bajo"):
            p.add_argument(f"--{inst}", help=f"canal(es) del {inst}: fm1..fm8")
        p.add_argument("--bateria", help="canal(es) de la batería: fm1..fm8, ruido, oki, segapcm, k053260, adpcm")
        p.add_argument("--drums", help="drums.json con el papel de cada grupo de la batería (si no existe, se escribe "
                                       "la sugerencia)" if name == "export-notes" else "drums.json con los papeles")
        p.set_defaults(func=cmd_export_notes if name == "export-notes" else cmd_channels)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
