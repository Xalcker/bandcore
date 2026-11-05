# bandcore

Lo que comparten las versiones de Band: [NESBand](https://github.com/Xalcker/NESBand),
[GBBand](https://github.com/Xalcker/GBBand), [GENBand](https://github.com/Xalcker/GENBand) y
[NGBand](https://github.com/Xalcker/NGBand).

Hoy trae:

- **Lector genérico de VGM** (`bandcore.vgm`): `.vgm` y `.vgz` de cualquier versión. Entrega los relojes de la
  cabecera, las escrituras a cualquier chip (también la memoria del SegaPCM), los bloques de datos y las ROM de los
  chips de samples, los streams de DAC, los eventos del DAC del YM2612 y las etiquetas GD3, sin interpretarlos. Cada
  juego decide qué hacer con eso (GENBand y NGBand ya leen así).
- **Detección del juego destino** (`bandcore.detect`): de los chips que suenan, la consola de origen y para qué
  juego sirve, en modo nativo (las escrituras tal cual) o con arreglo (las notas a otro chip).
- **Páginas del editor de charts** (`bandcore.editor`): `editor.html`, `home.html` y el menú Archivo, iguales en
  los cuatro juegos. Cada juego las sirve con su perfil (nombre, botones, canales del chip, textos de exportar e
  importar) y su reproductor para "Escuchar", que define `window.BandPlayer` (ver `src/bandcore/editor/__init__.py`).
- **Servidor del editor** (`bandcore.editor.server`): las rutas de la página (abrir, importar, validar, guardar,
  exportar, batería, Escuchar), solo en 127.0.0.1 y solo desde la propia página. Cada juego implementa una subclase de
  `EditorGame` con cómo carga, genera y exporta sus canciones.
- **Generador de charts** (`bandcore.chart`): la nota (`Note`, `ChartNote`), las dificultades, la rejilla de tiempos,
  reducir y repartir notas en carriles, acordes, colas, frases estelares y el formato de 2 bytes por entrada. Cada
  juego conserva solo cómo escribe sus charts (chart.s, .bin, song.c) y sus reglas extra (GBBand separa más las gemas).
- **Batería** (`bandcore.drums`): papeles, `drums.json` y el chart de batería. Agrupar los golpes y sugerir papeles
  depende de los canales de cada chip y sigue en cada juego.
- **Arreglos entre consolas** (`bandcore.notes`, `bandcore.famistudio`): un formato común de notas (guitarra,
  teclado, bajo y batería por papeles) que cada juego exporta con `<juego> export-notes`, y un escritor de proyectos
  de FamiStudio que NESBand, GBBand y NGBand importan (GENBand usa `audio/arreglo_fm.py`). Probado en las 12
  direcciones entre NES, Game Boy, Genesis y Neo Geo. Dos timbres, como en GENBand: **A** (guitarra, piano y bajo) y
  **B** (sintetizador, pulsada y bajo); en el NES y la Game Boy son duty y envolvente, en la Neo Geo el patch FM.
- **Canciones de arcade con YM2151** (`bandcore.ym2151`, `bandcore.pcm`, `bandcore.arcade`): ningún juego toca este
  chip, así que sus notas salen aquí, con `band export-notes`, para arreglarlas en cualquiera de las cuatro consolas.
  Lee los 8 canales FM y el ruido, aparta los canales FM de batería (barridos de tono) y los dobles (ecos y coros),
  y elige guitarra, teclado y bajo. La batería sale del chip de samples que acompaña al YM2151 (OKIM6295 del CPS1,
  SegaPCM de OutRun y Super Hang-On, K053260 de Konami, OKIM6258 del X68000) o, en el System 16, de sus canales FM.
  Cada sample se decodifica para sugerir su papel (un bombo cruza el cero poco, una tarola o un platillo mucho); la
  sugerencia queda en un `drums.json` para corregirla. `band channels` muestra los canales y la batería sugerida.

```bash
uv run band channels cancion.vgz                                   # canales, instrumentos y batería sugerida
uv run band export-notes cancion.vgz --out notas.json --drums drums.json   # escribe drums.json si no existe
uv run band export-notes cancion.vgz --out notas.json --drums drums.json --guitarra fm3 --bajo fm4
uv run band famistudio notas.json --out arreglo.txt               # y luego el import-song de cada juego
```

- **APU del NES** (`bandcore.nesapu`): el emulado que usan GBBand (NSF y VGM del NES) y NESBand (VGM del NES), con la
  grabación cuadro por cuadro y sus notas; `famistudio.write_recording_project` lo vuelve un proyecto fiel (duty y
  envolvente de cada nota).

```bash
uv run band famistudio notas.json --out arreglo.txt --timbre todos     # arreglo.txt (A) y arreglo_b.txt (B)
```
- **Ediciones de charts** (`bandcore.chartedit`): `edits.json`, aplicar y comparar ediciones y los avisos de lo que
  no se puede tocar. Con la nota (`bandcore.chart.ChartNote`) y las reglas de la cruz del control (`check_playable`).
  Leer los charts ya exportados (chart.s del NES, .bin de la Game Boy) sigue en cada juego.

- **Corredor de pruebas** (`bandcore.testrun`): lo que repetían los arneses de cada juego. Compila (`build.sh` o
  `build.ps1` según el sistema), lanza el emulador sin ventana (`Mame` para GENBand y NGBand, `Mesen` para NESBand),
  corre los casos en paralelo (`-j N`) con límite de tiempo, resume y devuelve el código de salida. Los cuatro juegos
  tienen el mismo comando, igual en macOS y Windows: `uv run <juego> test [--no-build] [--rapida] [-j N] [--lista]
  [patron]`. Cada juego pone sus casos y su revisor; las fallas ya conocidas van en `tests/known_failures.txt` (no
  cambian el código de salida, una falla nueva sí). GBBand usa pytest con PyBoy y solo toma de aquí las opciones y la
  compilación.

```bash
uv sync
uv run band inspect cancion.vgz          # chips, duración, bucle, etiquetas y destinos
uv run band inspect *.vgz --json
uv run pytest
```

| Chip del VGM | Consola | Destino nativo | Notas para arreglos |
|---|---|---|---|
| YM2612 + SN76489 | Mega Drive / Genesis | GENBand | `genband export-notes` |
| SN76489 | Master System / Game Gear | GENBand (solo PSG) | `genband export-notes` |
| YM2610 | Neo Geo | NGBand | `ngband export-notes` |
| GB DMG | Game Boy | GBBand | `gbband export-notes` |
| NES APU | NES | NESBand (sin DPCM ni FDS) | `gbband export-notes` |
| YM2151 (+ OKIM6295, SegaPCM, K053260, OKIM6258) | Arcade (CPS1, System 16, OutRun, Konami), X68000 | — | `band export-notes` |

Con las notas, cualquier consola sirve de destino: NESBand, GBBand y NGBand con `band famistudio`, y GENBand con
`audio/arreglo_fm.py`, los dos con timbre A o B. Las 12 direcciones entre las cuatro consolas están probadas
(importan con sus charts y suenan en el reproductor del editor).

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
