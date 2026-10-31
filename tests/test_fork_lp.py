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
async def test_start_stream_stops_stale_activity_then_starts(no_retry_delay):
    cam, device = _camera()
    order = []
    device.stop_activity.side_effect = lambda: order.append("stop") or True
    device.start_stream.side_effect = lambda ua: order.append(("start", ua)) or \
        "https://example.invalid/stream.mpd"
    result = await cam.async_start_fresh_stream("linux")
    assert order == ["stop", ("start", "linux")]
    assert result == {"url": "https://example.invalid/stream.mpd",
                      "format": "dash", "expires_in": 30, "reused": False, "attempts": 1}
    device.get_stream.assert_not_called()


@pytest.fixture
def no_retry_delay(monkeypatch):
    sleeps = []
    clock = SimpleNamespace(now=1000.0)

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(aarlo_camera.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(aarlo_camera.asyncio, "get_running_loop",
                        lambda: SimpleNamespace(time=lambda: clock.now))
    return sleeps


@pytest.mark.asyncio
async def test_start_stream_raises_when_arlo_returns_nothing(no_retry_delay):
    cam, device = _camera()
    device.start_stream.return_value = None
    with pytest.raises(HomeAssistantError):
        await cam.async_start_fresh_stream("arlo")
    # One retry, 4 s later, each attempt stopping first.
    assert device.start_stream.call_count == 2
    assert device.stop_activity.call_count == 2
    assert no_retry_delay == [4]


@pytest.mark.asyncio
async def test_active_stream_is_reused_without_stopping(no_retry_delay):
    cam, device = _camera(active=True)
    device.get_stream.return_value = "rtsps://example.invalid/live"
    result = await cam.async_start_fresh_stream("arlo")
    device.get_stream.assert_called_once_with("arlo")
    device.stop_activity.assert_not_called()
    device.start_stream.assert_not_called()
    assert result == {"url": "rtsps://example.invalid/live", "format": "rtsps",
                      "expires_in": 30, "reused": True, "attempts": 0}
    assert no_retry_delay == []


@pytest.mark.asyncio
async def test_active_stream_ignored_when_reuse_is_off(no_retry_delay):
    cam, device = _camera(active=True)
    device.start_stream.return_value = "rtsps://example.invalid/fresh"
    result = await cam.async_start_fresh_stream("arlo", reuse_active=False)
    device.get_stream.assert_not_called()
    device.stop_activity.assert_called_once_with()
    assert result["reused"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse_effect", [None, RuntimeError("boom")])
async def test_reuse_without_url_falls_back_to_fresh_start(no_retry_delay, reuse_effect):
    cam, device = _camera(active=True)
    if isinstance(reuse_effect, Exception):
        device.get_stream.side_effect = reuse_effect
    else:
        device.get_stream.return_value = reuse_effect
    order = []
    device.stop_activity.side_effect = lambda: order.append("stop") or True
    device.start_stream.side_effect = lambda ua: order.append("start") or         "rtsps://example.invalid/fresh"
    result = await cam.async_start_fresh_stream("arlo")
    assert order == ["stop", "start"]
    assert result["url"] == "rtsps://example.invalid/fresh"
    assert result["reused"] is False
    assert no_retry_delay == []


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [None, RuntimeError("cloud error")])
async def test_failed_fresh_start_is_retried_once(no_retry_delay, first):
    cam, device = _camera()
    order = []
    results = iter([first, "https://example.invalid/second.mpd"])

    def start(ua):
        order.append("start")
        value = next(results)
        if isinstance(value, Exception):
            raise value
        return value

    device.stop_activity.side_effect = lambda: order.append("stop") or True
    device.start_stream.side_effect = start
    result = await cam.async_start_fresh_stream("linux")
    assert order == ["stop", "start", "stop", "start"]
    assert no_retry_delay == [4]
    assert result["url"] == "https://example.invalid/second.mpd"
    assert result["reused"] is False


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
    assert data == {"entity_id": ["camera.aarlo_test"], "user_agent": "linux",
                    "reuse_active": True, "retry_for": 0, "watch_along": False}
    assert START_STREAM_SCHEMA({"entity_id": "camera.a", "retry_for": "40"})["retry_for"] == 40
    with pytest.raises(vol.Invalid):
        START_STREAM_SCHEMA({"entity_id": "camera.a", "retry_for": 61})
    assert START_STREAM_SCHEMA({"entity_id": "camera.a", "reuse_active": False})["reuse_active"] is False
    with pytest.raises(vol.Invalid):
        START_STREAM_SCHEMA({"entity_id": ["camera.a", "camera.b"]})


@pytest.mark.asyncio
async def test_service_handler_returns_the_camera_response(monkeypatch):
    cam = MagicMock()
    cam.async_start_fresh_stream = AsyncMock(return_value={"url": "u"})
    monkeypatch.setattr(aarlo_camera, "get_entity_from_domain", lambda *_: cam)
    call = SimpleNamespace(data={"entity_id": ["camera.aarlo_test"], "user_agent": "arlo",
                                 "reuse_active": False, "retry_for": 40, "watch_along": True})
    assert await aarlo_camera.async_camera_start_stream_service(None, call) == {"url": "u"}
    cam.async_start_fresh_stream.assert_awaited_once_with("arlo", False, 40, True)


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
    assert str(pyaarlo[0].specifier) == "==0.8.0.27.post3"


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


def test_stream_watchdog_option_reaches_pyaarlo():
    from pyaarlo.cfg import ArloCfg
    from custom_components.aarlo.cfg import AARLO_SCHEMA

    assert AARLO_SCHEMA({})["stream_watchdog_minutes"] == 15
    conf = AARLO_SCHEMA({"stream_watchdog_minutes": 0})
    assert conf["stream_watchdog_minutes"] == 0
    assert ArloCfg(MagicMock(), stream_watchdog_minutes=conf["stream_watchdog_minutes"]).stream_watchdog == 0
    assert ArloCfg(MagicMock()).stream_watchdog == 15 * 60
    with pytest.raises(vol.Invalid):
        AARLO_SCHEMA({"stream_watchdog_minutes": -1})


@pytest.mark.asyncio
async def test_retry_for_keeps_trying_without_stopping_until_the_call_ends(no_retry_delay):
    # Arlo refuses (409 / 14002, SIP call in progress) for the first 4 starts.
    cam, device = _camera()
    order = []
    answers = iter([None, None, None, None, "rtsps://example.invalid/after-call"])
    device.stop_activity.side_effect = lambda: order.append("stop") or True
    device.start_stream.side_effect = lambda ua: order.append("start") or next(answers)
    result = await cam.async_start_fresh_stream("arlo", reuse_active=False, retry_for=40)
    assert order == ["stop", "start", "stop", "start", "start", "start", "start"]
    assert no_retry_delay == [4, 3, 3, 3]
    assert result["url"] == "rtsps://example.invalid/after-call"
    assert result["attempts"] == 5


@pytest.mark.asyncio
async def test_retry_for_gives_up_at_the_deadline(no_retry_delay):
    cam, device = _camera()
    device.start_stream.return_value = None
    with pytest.raises(HomeAssistantError, match="attempts"):
        await cam.async_start_fresh_stream("arlo", reuse_active=False, retry_for=12)
    # 4 s before the second fresh start, then 3 s steps while they fit in 12 s.
    assert no_retry_delay == [4, 3, 3]
    assert device.start_stream.call_count == 4
    assert device.stop_activity.call_count == 2


@pytest.mark.asyncio
async def test_retry_for_zero_keeps_the_old_two_attempts(no_retry_delay):
    cam, device = _camera()
    device.start_stream.return_value = None
    with pytest.raises(HomeAssistantError):
        await cam.async_start_fresh_stream("arlo")
    assert device.start_stream.call_count == 2
    assert no_retry_delay == [4]


WATCH_URL = "rtsps://192.0.2.10:443/vzmodulelive/x?egressToken=T&watchalong=true"


@pytest.mark.asyncio
async def test_watch_along_only_queries_and_never_starts_or_stops(no_retry_delay):
    cam, device = _camera(active=True)
    device.get_stream_url.return_value = WATCH_URL
    result = await cam.async_start_fresh_stream("arlo", watch_along=True)
    device.get_stream_url.assert_called_once_with("arlo")
    device.stop_activity.assert_not_called()
    device.start_stream.assert_not_called()
    device.get_stream.assert_not_called()
    assert result == {"url": WATCH_URL, "format": "rtsps", "expires_in": 30,
                      "reused": False, "attempts": 1, "watch_along": True,
                      "watchalong_flag": True}
    assert no_retry_delay == []


@pytest.mark.asyncio
async def test_watch_along_retries_within_retry_for_without_stopping(no_retry_delay):
    cam, device = _camera()
    answers = iter([None, RuntimeError("409"), "rtsps://192.0.2.10/live"])

    def query(ua):
        value = next(answers)
        if isinstance(value, Exception):
            raise value
        return value

    device.get_stream_url.side_effect = query
    result = await cam.async_start_fresh_stream("arlo", watch_along=True, retry_for=20)
    assert device.get_stream_url.call_count == 3
    assert no_retry_delay == [3, 3]
    assert result["watchalong_flag"] is False
    assert result["attempts"] == 3
    device.stop_activity.assert_not_called()
    device.start_stream.assert_not_called()


@pytest.mark.asyncio
async def test_watch_along_gives_up_without_falling_back_to_a_start(no_retry_delay):
    cam, device = _camera()
    device.get_stream_url.return_value = None
    with pytest.raises(HomeAssistantError, match="watch-along"):
        await cam.async_start_fresh_stream("arlo", watch_along=True, retry_for=7)
    assert device.get_stream_url.call_count == 3  # t=0, 3, 6
    device.stop_activity.assert_not_called()
    device.start_stream.assert_not_called()


@pytest.mark.asyncio
async def test_watch_along_log_line_has_no_token(no_retry_delay, caplog):
    import logging
    cam, device = _camera()
    device.get_stream_url.return_value = WATCH_URL
    with caplog.at_level(logging.INFO, logger="custom_components.aarlo.camera"):
        await cam.async_start_fresh_stream("arlo", watch_along=True)
    assert "watchalong flag True" in caplog.text
    assert "egressToken" not in caplog.text and "192.0.2.10" not in caplog.text
