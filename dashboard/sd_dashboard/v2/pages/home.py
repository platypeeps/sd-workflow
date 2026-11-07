"""Home (sd:2117): the design's Home Assistant tiles, from the setting row or the config folder (sd:3987), with live
state (sd:3427). Edit tiles posts /api/home/tiles; a tile's Act section posts /api/home/service (sd:4966).
The Map view (pack sd:8053) reads /api/home/map and its aerial; its Add, move, edit and care post /api/home/asset,
/api/home/asset/delete and /api/home/care."""

from __future__ import annotations

from . import Api, Page, place_views


def _read(read):
    from ... import home_screen

    return home_screen.document(read.connection, now=read.now)


def _save(payload, principal):
    from ... import home_screen

    return home_screen.save(payload)


def _service(payload, principal):
    from ... import home_screen

    return home_screen.service(payload, principal)


def _map(read):
    from ... import home_map

    return home_map.document(read.connection, now=read.now)


def _aerial(read):
    from ... import home_map

    return home_map.aerial_route(read.connection)


def _asset(payload, principal):
    from ... import home_map

    return home_map.save(payload)


def _asset_delete(payload, principal):
    from ... import home_map

    return home_map.delete(payload)


def _care(payload, principal):
    from ... import home_map

    return home_map.care(payload, principal)


PAGE = Page(
    section="Home",
    routes=("/home",),
    html="home.html",
    item="sd:2117",
    api=(Api("/api/home", _read), Api("/api/home/tiles", write=_save), Api("/api/home/service", write=_service),
         # The property map (pack sd:8053, home_map).
         Api("/api/home/map", _map), Api("/api/home/map/aerial", _aerial), Api("/api/home/asset", write=_asset),
         Api("/api/home/asset/delete", write=_asset_delete), Api("/api/home/care", write=_care),
         *place_views("home")),
)
