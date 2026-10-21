"""Which Qt platform VM.start picks for window=True outside Windows."""

from __future__ import annotations

import pytest

from box86mcp.vm import _window_platform


@pytest.mark.parametrize("env, platform", [
    ({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}, "wayland;xcb"),
    ({"WAYLAND_DISPLAY": "wayland-0"}, "wayland"),
    ({"DISPLAY": ":0"}, "xcb"),
    ({"WAYLAND_DISPLAY": "", "DISPLAY": ":0"}, "xcb"),
])
def test_window_platform(env, platform):
    assert _window_platform(env) == platform


def test_window_platform_no_display():
    with pytest.raises(RuntimeError, match="WAYLAND_DISPLAY or DISPLAY"):
        _window_platform({})
