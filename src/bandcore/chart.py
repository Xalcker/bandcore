"""Lo que comparten los charts de las cuatro versiones: la nota (`ChartNote`) y las reglas de lo que se puede tocar.

El generador de charts (cómo salen las notas de la música) y el formato en que cada juego guarda sus charts siguen en
cada repo (`chart.py`); aquí va solo lo que el editor (`bandcore.chartedit`) y el generador deben entender igual.
"""

from __future__ import annotations

from dataclasses import dataclass

STAR_PHRASE_LEN = 4  # notas por frase de poder estelar (si aciertas todas, la barra sube 25 %)


@dataclass(frozen=True)
class ChartNote:
    frame: int  # cuadro en que suena (contando la entrada de la autopista)
    mask: int  # bits 0-4 = carriles
    end: int | None = None  # cuadro en que termina su cola (None = nota normal)
    star: bool = False  # nota de una frase de poder estelar
    cymbal: bool = False  # solo en la batería (no usa colas): el pad es un platillo, no un tom


# Los carriles verde, rojo y amarillo son izquierda, abajo y derecha de la cruz del control, que no deja apretar
# izquierda y derecha a la vez: verde + amarillo (carriles 0 y 2) es imposible. Con la opción ESPEJO los carriles se
# invierten (0 con 4, 1 con 3) y amarillo + naranja (2 y 4) pasaría a ser verde + amarillo. Por eso ningún chart pide
# esas dos parejas a la vez, ni en un acorde ni con una nota mientras se sostiene la otra.
BLOCKED_PAIRS = ({0, 2}, {2, 4})

# En la batería el bombo es el carril naranja pero se pisa con el pie, no con la cruz: ahí solo verde + amarillo (los
# dos toms de los extremos) es imposible. (ESPEJO no aplica a la batería.)
DRUM_BLOCKED_PAIRS = ({0, 2},)


def lanes_clash(a: set[int], b: set[int], blocked=BLOCKED_PAIRS) -> bool:
    """¿Dos notas con estos carriles no pueden estar activas al mismo tiempo? (mismo carril, o una pareja bloqueada)"""
    return bool(a & b) or any({x, y} in blocked for x in a for y in b)


def check_playable(chart: list[ChartNote], blocked=BLOCKED_PAIRS) -> None:
    """Falla si el chart pide algo imposible con la cruz del control: dos carriles de una pareja bloqueada
    (BLOCKED_PAIRS; en la batería DRUM_BLOCKED_PAIRS) a la vez, ya sea en un acorde o tocando una nota mientras se
    sostiene otra."""
    sets = [{b for b in range(5) if n.mask >> b & 1} for n in chart]
    for i, (note, lanes) in enumerate(zip(chart, sets)):
        if any({x, y} in blocked for x in lanes for y in lanes):
            raise ValueError(f"acorde imposible en el cuadro {note.frame}: carriles {sorted(lanes)}")
        if note.end is None:
            continue
        for other, other_lanes in zip(chart[i + 1 :], sets[i + 1 :]):
            if other.frame >= note.end:
                break
            if lanes_clash(other_lanes, lanes, blocked):
                raise ValueError(f"la nota del cuadro {other.frame} cae mientras se sostiene la del {note.frame}")
