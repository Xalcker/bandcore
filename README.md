# bandcore

Lo que comparten las versiones de Band: [NESBand](https://github.com/Xalcker/NESBand),
[GBBand](https://github.com/Xalcker/GBBand), [GENBand](https://github.com/Xalcker/GENBand) y
[NGBand](https://github.com/Xalcker/NGBand).

Hoy trae:

- **Lector genérico de VGM** (`bandcore.vgm`): `.vgm` y `.vgz` de cualquier versión. Entrega los relojes de la
  cabecera, las escrituras a cualquier chip, los bloques de datos, los eventos del DAC del YM2612 y las etiquetas
  GD3, sin interpretarlos. Cada juego decide qué hacer con eso (GENBand y NGBand ya leen así).
- **Detección del juego destino** (`bandcore.detect`): de los chips que suenan, la consola de origen y para qué
  juego sirve, en modo nativo (las escrituras tal cual) o con arreglo (las notas a otro chip).
- **Páginas del editor de charts** (`bandcore.editor`): `editor.html`, `home.html` y el menú Archivo, iguales en
  los cuatro juegos. Cada juego las sirve con su perfil (nombre, botones, canales del chip, textos de exportar e
  importar) y su reproductor para "Escuchar", que define `window.BandPlayer` (ver `src/bandcore/editor/__init__.py`).

```bash
uv sync
uv run band inspect cancion.vgz          # chips, duración, bucle, etiquetas y destinos
uv run band inspect *.vgz --json
uv run pytest
```

| Chip del VGM | Consola | Destino nativo |
|---|---|---|
| YM2612 + SN76489 | Mega Drive / Genesis | GENBand (listo) |
| SN76489 | Master System / Game Gear | GENBand (listo, solo PSG) |
| YM2610 | Neo Geo | NGBand (listo) |
| GB DMG | Game Boy | GBBand (pendiente) |
| NES APU | NES | NESBand (pendiente) |

## Uso desde un juego

Cada juego lo declara como dependencia de uv por ruta, así que se clona junto a ellos:

```toml
[project]
dependencies = ["bandcore"]

[tool.uv.sources]
bandcore = { path = "../bandcore", editable = true }
```

## Licencia

GPL-2.0-or-later, como los juegos.
