"""Para qué juego sirve un VGM: de los chips que suenan, la consola y los destinos posibles.

Un destino es "nativo" si el chip del VGM es el de la consola (se tocan sus escrituras tal cual y suena igual al
original) o "arreglo" si hay que pasar las notas a otro chip (con pérdida). Cada destino dice además si el juego ya
lo soporta hoy o si está pendiente.
"""

from __future__ import annotations

from dataclasses import dataclass

from .vgm import Vgm

NATIVE = "nativo"
ARRANGED = "arreglo"


@dataclass(frozen=True)
class Target:
    game: str  # "GENBand", "NGBand", "GBBand", "NESBand"
    mode: str  # NATIVE o ARRANGED
    ready: bool  # el juego ya lo importa
    note: str = ""


@dataclass(frozen=True)
class Report:
    system: str  # consola de origen ("Mega Drive / Genesis", "Neo Geo"...) o "desconocido"
    chips: list[str]  # los que suenan
    lost: list[str]  # los que suenan pero ningún destino toca
    targets: list[Target]


# Nombres para mostrar
CHIP_NAMES = {
    "sn76489": "SN76489 (PSG)",
    "ym2413": "YM2413 (OPLL)",
    "ym2612": "YM2612 (OPN2)",
    "ym2151": "YM2151 (OPM)",
    "ym2203": "YM2203 (OPN)",
    "ym2608": "YM2608 (OPNA)",
    "ym2610": "YM2610 (OPNB)",
    "ay8910": "AY-3-8910",
    "gb_dmg": "Game Boy (DMG)",
    "nes_apu": "NES APU",
    "huc6280": "HuC6280",
    "k051649": "Konami SCC",
    "pokey": "POKEY",
}


def chip_name(chip: str, vgm: Vgm | None = None) -> str:
    name = CHIP_NAMES.get(chip, chip.upper())
    flag = vgm.flags.get(chip, 0) if vgm else 0
    if chip == "ym2610" and flag & 2:
        name = "YM2610B (OPNB-B)"
    if chip == "nes_apu" and flag & 2:
        name += " + FDS"
    return name


def _system(chips: set[str], vgm: Vgm) -> str:
    if not chips:
        return "sin sonido"
    if chips <= {"ym2612", "sn76489"} and "ym2612" in chips:
        return "Mega Drive / Genesis"
    if chips == {"sn76489"}:
        return "Master System / Game Gear (o solo el PSG de Genesis)"
    if chips == {"ym2610"}:
        return "Neo Geo" if not vgm.flags.get("ym2610", 0) & 2 else "arcade (YM2610B, Taito y otros)"
    if chips == {"gb_dmg"}:
        return "Game Boy / Game Boy Color"
    if chips == {"nes_apu"}:
        return "NES / Famicom" + (" (con FDS)" if vgm.flags.get("nes_apu", 0) & 2 else "")
    if "ym2151" in chips:
        return "arcade (YM2151: CPS1, System 16 y otros)"
    return "desconocido"


def analyze(vgm: Vgm) -> Report:
    chips = vgm.chips()
    present = set(chips)
    targets: list[Target] = []
    covered: set[str] = set()

    if present & {"ym2612", "sn76489"}:
        covered |= present & {"ym2612", "sn76489"}
        note = "" if "ym2612" in present else "solo PSG: suena, pero sin FM"
        targets.append(Target("GENBand", NATIVE, True, note))
    if "ym2610" in present:
        covered.add("ym2610")
        b = vgm.flags.get("ym2610", 0) & 2
        targets.append(Target("NGBand", NATIVE, True, "YM2610B: los canales FM 1 y 4 no existen en la Neo Geo" if b else ""))
    if "gb_dmg" in present:
        covered.add("gb_dmg")
        targets.append(Target("GBBand", NATIVE, True))
    if "nes_apu" in present:
        covered.add("nes_apu")
        fds = "; el canal FDS se pierde" if vgm.flags.get("nes_apu", 0) & 2 else ""
        targets.append(Target("NESBand", NATIVE, False, "hoy importa NSF; falta leer el APU desde VGM" + fds))
        targets.append(Target("GBBand", ARRANGED, False, "GBBand ya arregla NES -> GB desde NSF; falta desde VGM" + fds))
    if "ym2151" in present:
        covered.add("ym2151")
        targets.append(Target("GENBand", ARRANGED, False, "FM de 4 operadores como el YM2612: traducir instrumentos"))

    # Arreglos por la IR (notas a otro chip): pendientes para cualquier origen con notas
    if covered:
        games = {t.game for t in targets}
        for game in ("GENBand", "NGBand", "GBBand", "NESBand"):
            if game not in games:
                targets.append(Target(game, ARRANGED, False, "por la IR: notas a otro chip"))

    lost = [c for c in chips if c not in covered]
    return Report(_system(present, vgm), chips, lost, targets)
