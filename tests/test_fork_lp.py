"""Fork (Jymdis/hass-aarlo) regressions: start-stream service, snapshot from
an active stream, ASCII ids by default and the pyaarlo requirement."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from awesomeversion import AwesomeVersion, AwesomeVersionException, AwesomeVersionStrategy
import voluptuous as vol
from homeassistant.components.ffmpeg import DATA_FFMPEG
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.package import parse_requirement_safe

from custom_components.aarlo import camera as aarlo_camera
from custom_components.aarlo.camera import ArloCam, START_STREAM_SCHEMA, stream_format
from custom_components.aarlo.const import NO_UNICODE_SQUASH

MANIFEST = Path(__file__).parents[1] / "custom_components" / "aarlo" / "manifest.json"


def _camera(config=None, active=False):
    device = MagicMock()
    device.name = "Test doorbell"
    device.entity_id = "test_doorbell"
    device.device_id = "synthetic-device"
    device.model_id = "synthetic-model"
    device.is_stream_active = active

    async def run(func, *args):
        return func(*args)

    hass = SimpleNamespace(data={DATA_FFMPEG: MagicMock()},
                           async_add_executor_job=AsyncMock(side_effect=run))
    cam = ArloCam(device, config or {}, hass)
    cam.hass = hass
    cam.stream = None
    return cam, device


@pytest.mark.parametrize("stream_snapshot", [False, True])
def test_snapshot_uses_the_active_stream_without_starting_another(stream_snapshot):
    cam, device = _camera({"stream_snapshot": stream_snapshot}, active=True)
    cam.request_snapshot()
    device.start_snapshot_stream.assert_not_called()
    device.wait_for_user_stream.assert_not_called()
    device.request_snapshot.assert_called_once_with()


def test_snapshot_without_active_stream_keeps_stream_snapshot_path():
    cam, device = _camera({"stream_snapshot": True}, active=False)
    device.start_snapshot_stream.return_value = "rtsps://example.invalid/s"
    cam.request_snapshot()
    device.start_snapshot_stream.assert_called_once_with()
    device.wait_for_user_stream.assert_called_once_with()
    device.request_snapshot.assert_called_once_with()


@pytest.mark.asyncio
async def test_start_stream_stops_stale_activity_then_starts():
    cam, device = _camera()
    order = []
    device.stop_activity.side_effect = lambda: order.append("stop") or True
    device.start_stream.side_effect = lambda ua: order.append(("start", ua)) or \
        "https://example.invalid/stream.mpd"
    result = await cam.async_start_fresh_stream("linux")
    assert order == ["stop", ("start", "linux")]
    assert result == {"url": "https://example.invalid/stream.mpd",
                      "format": "dash", "expires_in": 30}


@pytest.mark.asyncio
async def test_start_stream_raises_when_arlo_returns_nothing():
    cam, device = _camera()
    device.start_stream.return_value = None
    with pytest.raises(HomeAssistantError):
        await cam.async_start_fresh_stream("arlo")


@pytest.mark.parametrize("url,agent,expected", [
    ("rtsps://example.invalid/live", "arlo", "rtsps"),
    ("https://example.invalid/live/index.m3u8", "linux", "hls"),
    ("https://example.invalid/live/manifest.mpd?x=1", "linux", "dash"),
    ("https://example.invalid/live", "arlo", "rtsps"),
    ("https://example.invalid/live", "linux", "dash"),
])
def test_stream_format(url, agent, expected):
    assert stream_format(url, agent) == expected


def test_start_stream_schema_takes_one_camera_and_defaults_to_linux():
    data = START_STREAM_SCHEMA({"entity_id": "camera.aarlo_test"})
    assert data == {"entity_id": ["camera.aarlo_test"], "user_agent": "linux"}
    with pytest.raises(vol.Invalid):
        START_STREAM_SCHEMA({"entity_id": ["camera.a", "camera.b"]})


@pytest.mark.asyncio
async def test_service_handler_returns_the_camera_response(monkeypatch):
    cam = MagicMock()
    cam.async_start_fresh_stream = AsyncMock(return_value={"url": "u"})
    monkeypatch.setattr(aarlo_camera, "get_entity_from_domain", lambda *_: cam)
    call = SimpleNamespace(data={"entity_id": ["camera.aarlo_test"], "user_agent": "arlo"})
    assert await aarlo_camera.async_camera_start_stream_service(None, call) == {"url": "u"}
    cam.async_start_fresh_stream.assert_awaited_once_with("arlo")


def test_unicode_is_squashed_by_default():
    assert NO_UNICODE_SQUASH is False


def test_manifest_requirement_pins_the_fork_version():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    reqs = [parse_requirement_safe(r) for r in manifest["requirements"]]
    pyaarlo = [r for r in reqs if r is not None and r.name == "pyaarlo"]
    assert len(pyaarlo) == 1
    # A version pin (not a bare URL) lets HA see it as installed after the
    # first install instead of reinstalling from GitHub on every start.
    assert pyaarlo[0].url is None
    assert str(pyaarlo[0].specifier) == "==0.8.0.27.post1"


# The strategies homeassistant/loader.py accepts for a custom integration's
# manifest version; anything else blocks the integration from loading.
_HA_CUSTOM_VERSION_STRATEGIES = [
    AwesomeVersionStrategy.CALVER,
    AwesomeVersionStrategy.SEMVER,
    AwesomeVersionStrategy.SIMPLEVER,
    AwesomeVersionStrategy.BUILDVER,
    AwesomeVersionStrategy.PEP440,
]


def _ha_accepts(version):
    try:
        AwesomeVersion(version, ensure_strategy=_HA_CUSTOM_VERSION_STRATEGIES)
    except AwesomeVersionException:
        return False
    return True


def test_manifest_version_is_accepted_by_home_assistant():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert _ha_accepts(manifest["version"]), manifest["version"]
    # 0.8.1.27.lp1 was blocked by Home Assistant; the check must catch it.
    assert not _ha_accepts("0.8.1.27.lp1")
