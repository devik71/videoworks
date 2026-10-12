from __future__ import annotations

from videoworks.ingest import resolution_of


def _probe(stream: dict) -> dict:
    return {"streams": [{"codec_type": "video", "width": 640, "height": 360, **stream}]}


def test_resolution_plain() -> None:
    assert resolution_of(_probe({})) == (640, 360)


def test_resolution_follows_phone_rotation() -> None:
    """Телефон пише 640×360 з позначкою повороту — глядач бачить 360×640."""
    assert resolution_of(_probe({"side_data_list": [{"rotation": -90}, {}]})) == (360, 640)
    assert resolution_of(_probe({"tags": {"rotate": "270"}})) == (360, 640)
    assert resolution_of(_probe({"side_data_list": [{"rotation": 180}]})) == (640, 360)
