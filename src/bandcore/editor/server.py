"""Servidor local del editor de charts, el mismo en los cuatro juegos.

Sirve las páginas (editor.html, home.html) y la API que usan: abrir, cerrar e importar canciones, validar, guardar y
exportar charts, rehacer la batería con otra asignación y dar lo que el reproductor necesita para Escuchar. Lo propio
de cada juego (cómo carga una canción, cómo genera los charts automáticos, en qué formato exporta) lo pone una
subclase de `EditorGame`.

Rutas:
    GET  /               la página del editor (o la de inicio si no hay canción abierta)
    GET  /api/state      los datos de la canción abierta (lo mismo que trae la página)
    GET  /api/player     lo que necesita el reproductor de Escuchar
    GET  /api/songs      canciones de src/songs/
    GET  /api/sources    archivos que se pueden importar
    GET  /api/tracks     pistas del .m3u de una fuente (?source=...)
    POST /api/open       {name}
    POST /api/close
    POST /api/import     {source, name, title, game, number, duration, beat_frames}
    POST /api/validate   {song, key, chart}
    POST /api/save       {song, charts, drums}
    POST /api/export     {song, charts, drums}
    POST /api/drums      {song, assignment}

Solo escucha en 127.0.0.1, y los POST solo se aceptan desde la propia página (cabecera Origin).
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import EDITOR_HTML, HOME_HTML, render_page
from ..chartedit import chart_from_json

# Errores que se le muestran a la página (400) en lugar de tumbar la petición. SystemExit: los importadores de los
# juegos salen con un mensaje cuando un archivo no sirve.
USER_ERRORS = (ValueError, KeyError, TypeError, OSError, SystemExit)


class EditorGame:
    """Lo que cada juego le pone al editor. Una canción (`song`) es el objeto que cada juego use (tiene `.name`)."""

    profile: dict  # el perfil de la página (ver bandcore.editor)

    def player_js(self) -> str:
        """La emulación del chip y el adaptador que define window.BandPlayer."""
        return ""

    def render(self, payload: dict, template=EDITOR_HTML) -> str:
        return render_page(payload, self.profile, self.player_js(), template)

    # --- La canción abierta ---
    def open(self, name: str) -> Any:
        """Carga la canción `name` de src/songs/ (ValueError si no existe)."""
        raise NotImplementedError

    def payload(self, song) -> dict:
        """Los datos de la página: canales, charts automáticos y editados, batería..."""
        raise NotImplementedError

    def player(self, song) -> dict:
        """Lo que responde /api/player."""
        raise NotImplementedError

    def problems(self, key: str, chart) -> list[dict]:
        raise NotImplementedError

    def drums(self, song, assignment: dict) -> dict:
        """Los charts de batería rehechos con otra asignación: {charts, auto, problems, warnings}."""
        raise NotImplementedError

    def save(self, song, charts: dict, assignment: dict | None) -> dict:
        """Guarda edits.json (y drums.json); devuelve las ediciones por chart."""
        raise NotImplementedError

    def export(self, song, charts: dict, assignment: dict | None) -> tuple[str, Any]:
        """Guarda y exporta los charts al formato del juego: (mensaje, la canción como queda después)."""
        raise NotImplementedError

    # --- El menú Archivo ---
    def songs(self) -> list[dict]:
        """Canciones de src/songs/: [{name, title, game, edited}]."""
        raise NotImplementedError

    def sources(self) -> list[dict]:
        """Archivos que se pueden importar: [{path, name}] (vacío si el juego no importa desde el editor)."""
        return []

    def tracks(self, source: str) -> dict:
        return {"game": None, "tracks": []}

    def import_song(self, body: dict) -> tuple[Any, str]:
        """Importa una canción nueva: (la canción, el resumen de la importación)."""
        raise ValueError(f"importa las canciones con `{self.profile['cli']} import-song`")


class App:
    """El juego y la canción abierta (o ninguna). Un candado evita que dos acciones se pisen."""

    def __init__(self, game: EditorGame, song=None):
        self.game = game
        self.song = song
        self.lock = threading.RLock()


class Handler(BaseHTTPRequestHandler):
    app: App  # lo fija make_server (o una subclase en las pruebas)

    def log_message(self, format, *args):  # sin ruido en la terminal
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json")

    def _error(self, error: BaseException) -> None:
        self._json(400, {"error": str(getattr(error, "code", None) or error)})

    def _song(self, body: dict | None = None):
        song = self.app.song
        if song is None:
            raise ValueError("no hay canción abierta")
        if body is not None and body.get("song") != song.name:
            raise ValueError("la canción de esta página ya no es la abierta: recarga la página")
        return song

    def do_GET(self) -> None:
        url = urlparse(self.path)
        app, game = self.app, self.app.game
        try:
            with app.lock:
                if url.path == "/":
                    if app.song is None:
                        page = game.render({"home": True}, HOME_HTML)
                    else:
                        page = game.render({**game.payload(app.song), "editable": True, "player": True})
                    self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                elif url.path == "/api/state":
                    self._json(200, {**game.payload(self._song()), "editable": True})
                elif url.path == "/api/player":
                    self._json(200, game.player(self._song()))
                elif url.path == "/api/songs":
                    self._json(200, {"songs": game.songs()})
                elif url.path == "/api/sources":
                    self._json(200, {"sources": game.sources()})
                elif url.path == "/api/tracks":
                    self._json(200, game.tracks(parse_qs(url.query).get("source", [""])[0]))
                else:
                    self._send(404, b"no encontrado", "text/plain; charset=utf-8")
        except USER_ERRORS as error:
            self._error(error)

    def do_POST(self) -> None:
        # Solo la propia página: otro origen (una web cualquiera) no puede escribir en el proyecto
        if self.headers.get("Origin") not in (None, f"http://{self.headers.get('Host')}"):
            self._json(403, {"error": "origen no permitido"})
            return
        app, game = self.app, self.app.game
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("cuerpo inválido")
            with app.lock:
                if self.path == "/api/open":
                    app.song = game.open(str(body.get("name", "")))
                    self._json(200, {"message": f"abierta {app.song.name}"})
                elif self.path == "/api/close":
                    app.song = None
                    self._json(200, {"message": "cerrada"})
                elif self.path == "/api/import":
                    app.song, message = game.import_song(body)
                    self._json(200, {"message": message})
                elif self.path == "/api/validate":
                    self._song(body)
                    self._json(200, {"problems": game.problems(body["key"], chart_from_json(body["chart"]))})
                elif self.path == "/api/drums":
                    self._json(200, game.drums(self._song(body), body["assignment"]))
                elif self.path in ("/api/save", "/api/export"):
                    song = self._song(body)
                    charts = {key: chart_from_json(items) for key, items in body["charts"].items()}
                    assignment = body.get("drums")
                    if self.path == "/api/save":
                        edits = game.save(song, charts, assignment)
                        self._json(200, {"message": f"guardado ({sum(len(ops) for ops in edits.values())} ediciones)"})
                    else:
                        message, app.song = game.export(song, charts, assignment)
                        self._json(200, {"message": message})
                else:
                    self._json(404, {"error": "no encontrado"})
        except USER_ERRORS as error:
            self._error(error)


def make_server(app: App, port: int = 0) -> ThreadingHTTPServer:
    """El servidor, ligado a 127.0.0.1 (nunca a otra interfaz)."""
    return ThreadingHTTPServer(("127.0.0.1", port), type("Bound", (Handler,), {"app": app}))


def serve(app: App, port: int, open_browser: bool) -> None:
    server = make_server(app, port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    name = app.song.name if app.song is not None else None
    print(f"Editor de charts{f' de {name!r}' if name else ''}: {url}  (Ctrl+C para cerrar)")
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
