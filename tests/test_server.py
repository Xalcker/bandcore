"""Pruebas del servidor compartido del editor, con un juego de mentira."""

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass

import pytest

from bandcore.chart import ChartNote
from bandcore.editor import server
from test_editor import PLAYER, PROFILE


@dataclass
class Song:
    name: str


class FakeGame(server.EditorGame):
    profile = PROFILE

    def __init__(self):
        self.saved = None

    def player_js(self):
        return PLAYER

    def open(self, name):
        if name not in ("uno", "dos"):
            raise ValueError(f"no existe la canción {name}")
        return Song(name)

    def payload(self, song):
        return {"name": song.name, "charts": {}}

    def player(self, song):
        return {"stream": "", "frames": 1}

    def problems(self, key, chart):
        return [{"frame": n.frame} for n in chart if n.mask > 0x1F]

    def save(self, song, charts, assignment):
        self.saved = (song.name, charts, assignment)
        return {key: [] for key in charts}

    def export(self, song, charts, assignment):
        self.save(song, charts, assignment)
        return "exportado", Song(song.name)

    def songs(self):
        return [{"name": "uno", "title": "Uno", "game": "", "edited": False}]


@pytest.fixture
def running():
    app = server.App(FakeGame())
    httpd = server.make_server(app, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield app, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(url, body=None, origin=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method="GET" if body is None else "POST")
    if origin:
        request.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(request) as response:
            text = response.read().decode()
            return response.status, json.loads(text) if response.headers.get_content_type() == "application/json" else text
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_home_open_and_page(running):
    app, url = running
    status, page = call(url + "/")
    assert status == 200 and '"home":true' in page
    assert call(url + "/api/songs")[1]["songs"][0]["name"] == "uno"
    assert call(url + "/api/player")[0] == 400  # sin canción abierta
    assert call(url + "/api/open", {"name": "uno"}) == (200, {"message": "abierta uno"})
    status, page = call(url + "/")
    assert '"name":"uno"' in page and '"editable":true' in page and '"player":true' in page
    assert call(url + "/api/state")[1]["editable"] is True
    assert call(url + "/api/open", {"name": "../etc"})[0] == 400


def test_save_export_and_stale_page(running):
    app, url = running
    call(url + "/api/open", {"name": "uno"})
    charts = {"guitarra/facil": [{"frame": 10, "mask": 1, "end": None, "star": False}]}
    assert call(url + "/api/save", {"song": "uno", "charts": charts}) == (200, {"message": "guardado (0 ediciones)"})
    assert app.game.saved[1]["guitarra/facil"] == [ChartNote(10, 1)]
    status, data = call(url + "/api/export", {"song": "uno", "charts": charts})
    assert (status, data["message"]) == (200, "exportado") and app.song == Song("uno")
    # Otra canción abierta mientras tanto: la página vieja no puede guardar
    call(url + "/api/open", {"name": "dos"})
    status, data = call(url + "/api/save", {"song": "uno", "charts": charts})
    assert status == 400 and "recarga" in data["error"]


def test_import_without_support_and_other_origin(running):
    _, url = running
    status, data = call(url + "/api/import", {"name": "x", "source": "x.vgm"})
    assert status == 400 and "pruebaband import-song" in data["error"]
    assert call(url + "/api/close", {}, origin="http://otra.web")[0] == 403
    assert call(url + "/api/close", {}, origin=url.replace("http://", "http://"))[0] == 200
    assert call(url + "/api/sources")[1] == {"sources": []}
