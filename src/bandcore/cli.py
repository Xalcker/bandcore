"""Línea de comandos de bandcore.

    uv run band inspect cancion.vgz [otra.vgm ...] [--json]
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
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
