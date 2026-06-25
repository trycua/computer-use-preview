# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Native-desktop `Computer` environment, powered by cua-driver.

The browser backends in this repo (playwright, browserbase) drive *web pages*.
This backend drives a *native desktop application* (Windows / macOS / Linux) via
**cua-driver** (https://github.com/trycua/cua) — a background automation daemon.

Why it's interesting for Gemini computer-use:
  - reaches native apps the browser backends can't touch;
  - **window-scoped + background**: actions target one window's pixels without
    raising it or stealing the user's foreground/cursor;
  - **zoom → pixel-accurate grounding**: cua-driver's `zoom` crops a window
    region to a JPEG and round-trips clicks back through the inverse transform.

Mapping to Gemini's built-in computer tool:
  The model emits absolute (x, y) scaled to `screen_size()`. We make
  `screen_size()` the TARGET WINDOW's size and feed the model that window's
  screenshot, so the model's coords are window-local and map straight to
  cua-driver clicks against `window_id`. (A whole-display mode and a zoom mode
  are documented extensions — see TODOs.)

STATUS: scaffold / WIP. Decisions still open (flagged `# DECIDE:`):
  1. Transport to cua-driver — CLI `cua-driver call <tool>` (default here) vs the
     MCP stdio server vs the cua-computer Python SDK.
  2. The screenshot/coords zoom contract (`from_zoom`) for the zoom mode.
  3. App launch/attach lifecycle.
The cua-driver tool names + arg shapes below match the driver's tools but the
exact `call` output schema must be verified against a live `cua-driver`.
"""

import base64
import json
import os
import shutil
import subprocess
import time
from typing import Any, Literal

from ..computer import Computer, EnvState


# DECIDE: transport. This default shells out to the `cua-driver` binary's
# per-call CLI (`cua-driver call <tool> '<json>'`) — the most transparent
# "these are cua-driver's tools" surface. Swap the body of `_CuaDriver._call`
# for the MCP stdio client or the cua-computer SDK without touching the mapping.
class _CuaDriver:
    """Thin client over the cua-driver tool surface (single transport chokepoint)."""

    def __init__(self, binary: str | None = None):
        self.binary = binary or os.environ.get("CUA_DRIVER_BIN", "cua-driver")
        if shutil.which(self.binary) is None:
            raise RuntimeError(
                f"cua-driver binary '{self.binary}' not found on PATH. Install it: "
                "https://github.com/trycua/cua (or set CUA_DRIVER_BIN)."
            )

    def _call(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = json.dumps(args or {})
        # cua-driver reads the JSON args from stdin (see the driver docs) so we
        # avoid shell-quoting the payload.
        proc = subprocess.run(
            [self.binary, "call", tool],
            input=payload,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"cua-driver call {tool} failed: {proc.stderr.strip()}")
        out = proc.stdout.strip()
        try:
            return json.loads(out) if out else {}
        except json.JSONDecodeError:
            # Some tools print a human line + structured content; keep the raw.
            return {"_raw": out}


class DesktopComputer(Computer):
    """A native-desktop `Computer` env driven by cua-driver, scoped to one window."""

    def __init__(
        self,
        screen_size: tuple[int, int],
        app: str | None = None,
        window_title: str | None = None,
        driver_binary: str | None = None,
    ):
        # screen_size is the coordinate space we advertise to the model. We
        # reconcile it to the resolved window's size on enter.
        self._screen_size = screen_size
        self._app = app  # app/aumid/bundle-id/exec to launch or attach to
        self._window_title = window_title
        self._driver = _CuaDriver(driver_binary)
        self._pid: int | None = None
        self._window_id: int | None = None
        self._title: str = window_title or (app or "desktop")

    # ── lifecycle ────────────────────────────────────────────────────────────
    def __enter__(self):
        # DECIDE: launch vs attach. If `app` is set, launch it; then resolve the
        # target window via list_windows (by title if given, else first window).
        if self._app:
            self._driver._call("launch_app", {"app": self._app})
            time.sleep(2.0)  # TODO: poll for the window instead of sleeping
        self._resolve_window()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Leave the app running by default (background automation); nothing to
        # tear down on the driver side for a per-call CLI transport.
        return False

    def _resolve_window(self) -> None:
        result = self._driver._call("list_windows", {})
        windows = result.get("windows") or result.get("structuredContent", {}).get(
            "windows", []
        )
        if not windows:
            raise RuntimeError("cua-driver list_windows returned no windows.")
        target = windows[0]
        if self._window_title:
            target = next(
                (w for w in windows if self._window_title.lower() in (w.get("title") or "").lower()),
                windows[0],
            )
        self._pid = target.get("pid")
        self._window_id = target.get("window_id") or target.get("hwnd") or target.get("xid")
        self._title = target.get("title") or self._title
        # Advertise the window's real size as our coordinate space if available.
        w, h = target.get("width"), target.get("height")
        if w and h:
            self._screen_size = (int(w), int(h))

    def _win(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        d: dict[str, Any] = {"pid": self._pid, "window_id": self._window_id}
        if extra:
            d.update(extra)
        return d

    # ── state / screenshot ───────────────────────────────────────────────────
    def screen_size(self) -> tuple[int, int]:
        return self._screen_size

    def take_screenshot(self) -> EnvState:
        return self.current_state()

    def current_state(self) -> EnvState:
        # Window-scoped screenshot so the model's coords are window-local.
        res = self._driver._call("screenshot", self._win())
        b64 = (
            res.get("image_data")
            or res.get("structuredContent", {}).get("image_data")
            or ""
        )
        png = base64.b64decode(b64) if b64 else b""
        # EnvState.url is required; surface the window identity for a desktop env.
        return EnvState(screenshot=png, url=f"desktop://{self._title}")

    # ── pointer ──────────────────────────────────────────────────────────────
    def click_at(self, x: int, y: int) -> EnvState:
        self._driver._call("click", self._win({"x": x, "y": y}))
        return self.current_state()

    def double_click_at(self, x: int, y: int) -> EnvState:
        self._driver._call("double_click", self._win({"x": x, "y": y}))
        return self.current_state()

    def triple_click_at(self, x: int, y: int) -> EnvState:
        # cua-driver click supports a count; triple = count:3.
        self._driver._call("click", self._win({"x": x, "y": y, "count": 3}))
        return self.current_state()

    def middle_click_at(self, x: int, y: int) -> EnvState:
        self._driver._call("click", self._win({"x": x, "y": y, "button": 2}))
        return self.current_state()

    def right_click_at(self, x: int, y: int) -> EnvState:
        self._driver._call("right_click", self._win({"x": x, "y": y}))
        return self.current_state()

    def mouse_down(self, x: int, y: int) -> EnvState:
        # TODO: map to a cua-driver press/hold tool (drag primitives).
        raise NotImplementedError("mouse_down: pending cua-driver press/hold mapping")

    def mouse_up(self, x: int, y: int) -> EnvState:
        raise NotImplementedError("mouse_up: pending cua-driver press/hold mapping")

    def hover_at(self, x: int, y: int) -> EnvState:
        self._driver._call("move_cursor", self._win({"x": x, "y": y}))
        return self.current_state()

    def drag_and_drop(self, x: int, y: int, destination_x: int, destination_y: int) -> EnvState:
        self._driver._call(
            "drag",
            self._win({"from_x": x, "from_y": y, "to_x": destination_x, "to_y": destination_y}),
        )
        return self.current_state()

    # ── keyboard ─────────────────────────────────────────────────────────────
    def type_text(self, text: str, press_enter: bool = False) -> EnvState:
        self._driver._call("type_text", self._win({"text": text}))
        if press_enter:
            self._driver._call("press_key", self._win({"key": "enter"}))
        return self.current_state()

    def type_text_at(
        self, x: int, y: int, text: str, press_enter: bool, clear_before_typing: bool
    ) -> EnvState:
        self._driver._call("click", self._win({"x": x, "y": y}))
        if clear_before_typing:
            self._driver._call("press_key", self._win({"key": "ctrl+a"}))
            self._driver._call("press_key", self._win({"key": "delete"}))
        self._driver._call("type_text", self._win({"text": text}))
        if press_enter:
            self._driver._call("press_key", self._win({"key": "enter"}))
        return self.current_state()

    def key_combination(self, keys: list[str]) -> EnvState:
        self._driver._call("press_key", self._win({"key": "+".join(keys)}))
        return self.current_state()

    def press_key(self, key: str) -> EnvState:
        self._driver._call("press_key", self._win({"key": key}))
        return self.current_state()

    def key_down(self, key: str) -> EnvState:
        raise NotImplementedError("key_down: pending cua-driver key-hold mapping")

    def key_up(self, key: str) -> EnvState:
        raise NotImplementedError("key_up: pending cua-driver key-hold mapping")

    # ── scroll ───────────────────────────────────────────────────────────────
    def scroll_document(self, direction: Literal["up", "down", "left", "right"]) -> EnvState:
        w, h = self._screen_size
        self._driver._call("scroll", self._win({"x": w // 2, "y": h // 2, "direction": direction, "amount": 5}))
        return self.current_state()

    def scroll_at(
        self, x: int, y: int, direction: Literal["up", "down", "left", "right"], magnitude: int
    ) -> EnvState:
        self._driver._call("scroll", self._win({"x": x, "y": y, "direction": direction, "amount": magnitude}))
        return self.current_state()

    # ── waits ────────────────────────────────────────────────────────────────
    def wait(self, seconds: int) -> EnvState:
        time.sleep(seconds)
        return self.current_state()

    def wait_5_seconds(self) -> EnvState:
        return self.wait(5)

    # ── browser-only verbs: adapted for a desktop env ────────────────────────
    # The interface is browser-shaped. On a native desktop these are no-ops or
    # not meaningful; we keep them so the env satisfies the abstract Computer.
    def open_web_browser(self) -> EnvState:
        # On desktop the "environment" is the app, already launched on enter.
        return self.current_state()

    def navigate(self, url: str) -> EnvState:
        raise NotImplementedError("navigate: not applicable to a native-desktop env")

    def go_back(self) -> EnvState:
        raise NotImplementedError("go_back: not applicable to a native-desktop env")

    def go_forward(self) -> EnvState:
        raise NotImplementedError("go_forward: not applicable to a native-desktop env")

    def search(self) -> EnvState:
        raise NotImplementedError("search: not applicable to a native-desktop env")
