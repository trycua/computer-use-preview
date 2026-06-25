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
**Cua Driver** (https://github.com/trycua/cua) — a background automation daemon
that posts input to a single window without raising it or stealing the user's
foreground / cursor.

Why it's interesting for Gemini computer-use:
  - reaches native apps the browser backends can't touch (KiCad, CAD, legacy
    desktop tools);
  - **window-scoped + background**: actions target one window's pixels without
    bringing it to the foreground or moving the real mouse;
  - **zoom → pixel-accurate grounding**: Cua Driver's `zoom` tool crops a window
    region to a JPEG and round-trips clicks back through the inverse transform
    (`from_zoom=true`) — the capability behind Cua's KiCad benchmark results.

Mapping to Gemini's built-in computer tool:
  The model emits absolute (x, y) scaled to `screen_size()`. We make
  `screen_size()` the TARGET WINDOW's screenshot size and feed the model that
  window's screenshot, so the model's coords are window-local screenshot pixels
  — exactly the space Cua Driver's `click(x, y, window_id=...)` consumes. The
  driver handles window-local→screen translation and Retina backing-scale
  internally.

cua-driver tool surface this maps onto (verified against
libs/cua-driver/rust/crates/*/tools — June 2026):
  - screenshot         → get_window_state(capture_mode="vision")  [there is NO
                         standalone `screenshot` tool; it was removed in #1692]
  - click / dbl / right→ click / double_click / right_click  (pixel path: x, y,
                         window_id; button="left|right|middle"; count for triple)
  - type               → type_text(pid, text)  (AX value-set, CGEvent fallback)
  - press_key          → press_key(pid, key, modifiers=[...])
  - scroll             → scroll(pid, direction, by="line|page", amount)
                         [keystroke-based — does NOT take x, y]
  - drag               → drag(pid, from_x, from_y, to_x, to_y, window_id)
  - hover              → move_cursor (agent-cursor overlay only — see note)
  - list/launch        → list_windows / launch_app(name|bundle_id|aumid)

Transport: shells out to the `cua-driver` CLI's per-call surface
(`cua-driver call <tool>`, args piped as JSON on stdin). This is the most
transparent "these are cua-driver's tools" surface; swap `_CuaDriver._call`
for the MCP stdio client or the cua-computer SDK without touching the mapping.

OPEN ITEMS (documented, intentionally out of scope for this first cut):
  - `zoom` / `from_zoom` coordinate round-trip: not wired here. To use it the
    bridge must track "is the model currently looking at a zoom JPEG?" and set
    `from_zoom=true` on the resulting click. See README "Open items".
  - `mouse_down` / `mouse_up` and `key_down` / `key_up`: cua-driver exposes
    press-hold via the `drag` gesture and `mouse_button_down/up` (not on every
    platform); raised as NotImplementedError here.
  - `move_cursor` is an agent-cursor *overlay* move on macOS/Windows, not a real
    OS pointer move, so `hover_at` won't trigger native :hover styling there.
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Literal

from ..computer import Computer, EnvState


class _CuaDriver:
    """Thin client over the cua-driver CLI tool surface (single transport chokepoint).

    `cua-driver call <tool>` reads JSON args from stdin and prints the tool's
    result to stdout. The output shape (verified in
    crates/cua-driver/src/cli.rs::run_call) is:

      * if the tool returns `structuredContent` → pretty-printed JSON of it;
        any image content part is merged in under the key `screenshot_png_b64`
        (+ `screenshot_mime_type`) when `--screenshot-out-file` is not used.
      * if the tool returns text only (most action tools) → the plain text
        summary line, NOT JSON.
      * errors → text on stderr, process exit code 1.
    """

    def __init__(self, binary: str | None = None):
        self.binary = binary or os.environ.get("CUA_DRIVER_BIN", "cua-driver")
        if shutil.which(self.binary) is None:
            raise RuntimeError(
                f"cua-driver binary '{self.binary}' not found on PATH. Cua Driver "
                "is a native binary (not a pip package); install it from "
                "https://github.com/trycua/cua, or set CUA_DRIVER_BIN to its path."
            )

    def call(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Invoke a tool, returning parsed JSON (structuredContent) or {"_text": ...}."""
        payload = json.dumps(args or {})
        proc = subprocess.run(
            [self.binary, "call", tool],
            input=payload,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"cua-driver call {tool} failed (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()}"
            )
        out = proc.stdout.strip()
        if not out:
            return {}
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            # Text-only tool (click/type/scroll/...). Surface the line; callers
            # that need structured data (screenshot/list_windows) get JSON.
            return {"_text": out}


class DesktopComputer(Computer):
    """A native-desktop `Computer` env driven by cua-driver, scoped to one window."""

    def __init__(
        self,
        screen_size: tuple[int, int],
        app: str | None = None,
        bundle_id: str | None = None,
        window_title: str | None = None,
        driver_binary: str | None = None,
    ):
        # screen_size is the coordinate space we advertise to the model; it is
        # reconciled to the resolved window's screenshot size on enter so the
        # model's 0-999 coords de-normalize against the same image we send it.
        self._screen_size = screen_size
        self._app = app  # display name (or, on Windows, an AUMID) to launch
        self._bundle_id = bundle_id  # macOS bundle id / Windows AUMID, preferred when set
        self._window_title = window_title
        self._driver = _CuaDriver(driver_binary)
        self._pid: int | None = None
        self._window_id: int | None = None
        self._title: str = window_title or app or bundle_id or "desktop"

    # ── lifecycle ────────────────────────────────────────────────────────────
    def __enter__(self):
        # Launch (or attach to) the target app. launch_app is idempotent and
        # returns the app's `windows` array in the same call, so we can resolve
        # the window without a second round-trip in the common case.
        launched_windows: list[dict[str, Any]] = []
        if self._app or self._bundle_id:
            launch_args: dict[str, Any] = {}
            if self._bundle_id:
                # bundle_id covers macOS bundle ids and Windows AUMIDs.
                launch_args["bundle_id"] = self._bundle_id
            if self._app:
                launch_args["name"] = self._app
            result = self._driver.call("launch_app", launch_args)
            self._pid = result.get("pid")
            launched_windows = result.get("windows") or []
            if not launched_windows:
                # WindowServer can lag LaunchServices; give it a moment then
                # fall back to list_windows below.
                time.sleep(1.5)
        self._resolve_window(prelisted=launched_windows)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Leave the app running by default (background automation); the per-call
        # CLI transport holds no daemon-side session state to tear down.
        return False

    def _resolve_window(self, prelisted: list[dict[str, Any]] | None = None) -> None:
        windows = prelisted or []
        if not windows:
            # Filter to our pid when we have one, to avoid grabbing an unrelated
            # foreground window.
            args = {"pid": self._pid} if self._pid else {}
            result = self._driver.call("list_windows", args)
            windows = result.get("windows") or []
        if not windows:
            raise RuntimeError(
                "cua-driver returned no windows for the target app. Make sure the "
                "app is running and has an open window (and on macOS that Cua Driver "
                "has Accessibility + Screen Recording TCC permissions)."
            )

        if self._window_title:
            target = next(
                (
                    w
                    for w in windows
                    if self._window_title.lower() in (w.get("title") or "").lower()
                ),
                windows[0],
            )
        else:
            # Prefer an on-screen, titled window; fall back to the first.
            target = next(
                (w for w in windows if w.get("title") and w.get("is_on_screen", True)),
                windows[0],
            )

        self._pid = target.get("pid", self._pid)
        self._window_id = target.get("window_id")
        if self._window_id is None:
            raise RuntimeError(f"Resolved window has no window_id: {target!r}")
        self._title = target.get("title") or self._title

        # Advertise the screenshot size as our coordinate space. We take a real
        # screenshot to get the exact pixel dims (which may be downscaled by the
        # driver's max_image_dimension); fall back to window bounds otherwise.
        sw, sh = self._screenshot_dims()
        if sw and sh:
            self._screen_size = (int(sw), int(sh))
        else:
            # cua-driver list_windows nests geometry under `bounds` on every
            # platform (trycua/cua#2018 normalized Linux to match macOS/Windows).
            bounds = target.get("bounds") or {}
            w, h = bounds.get("width"), bounds.get("height")
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

    def _screenshot_dims(self) -> tuple[int | None, int | None]:
        res = self._driver.call(
            "get_window_state", self._win({"capture_mode": "vision"})
        )
        return res.get("screenshot_width"), res.get("screenshot_height")

    def take_screenshot(self) -> EnvState:
        return self.current_state()

    def current_state(self) -> EnvState:
        # Window-scoped screenshot via get_window_state(capture_mode="vision").
        # There is no standalone `screenshot` tool (removed in cua PR #1692).
        # The CLI merges the PNG into structuredContent under `screenshot_png_b64`.
        res = self._driver.call(
            "get_window_state", self._win({"capture_mode": "vision"})
        )
        b64 = res.get("screenshot_png_b64", "")
        png = base64.b64decode(b64) if b64 else b""
        # Keep the advertised coordinate space in sync with the image we return.
        sw, sh = res.get("screenshot_width"), res.get("screenshot_height")
        if sw and sh:
            self._screen_size = (int(sw), int(sh))
        # EnvState.url is required by the interface; surface the window identity.
        return EnvState(screenshot=png, url=f"desktop://{self._title}")

    # ── pointer ──────────────────────────────────────────────────────────────
    # Pixel coords are window-local screenshot pixels (top-left origin) — the
    # same space current_state() returns. The driver translates window-local→
    # screen and handles Retina backing-scale itself.
    def click_at(self, x: int, y: int) -> EnvState:
        self._driver.call("click", self._win({"x": x, "y": y}))
        return self.current_state()

    def double_click_at(self, x: int, y: int) -> EnvState:
        self._driver.call("double_click", self._win({"x": x, "y": y}))
        return self.current_state()

    def triple_click_at(self, x: int, y: int) -> EnvState:
        # click takes a `count` (pixel path); triple = count:3.
        self._driver.call("click", self._win({"x": x, "y": y, "count": 3}))
        return self.current_state()

    def middle_click_at(self, x: int, y: int) -> EnvState:
        self._driver.call("click", self._win({"x": x, "y": y, "button": "middle"}))
        return self.current_state()

    def right_click_at(self, x: int, y: int) -> EnvState:
        self._driver.call("right_click", self._win({"x": x, "y": y}))
        return self.current_state()

    def mouse_down(self, x: int, y: int) -> EnvState:
        # cua-driver models press-hold via the `drag` gesture (and a
        # `mouse_button_down`/`up` pair on some platforms). A standalone
        # down/up split isn't wired in this first cut.
        raise NotImplementedError(
            "mouse_down: cua-driver press-hold (drag / mouse_button_down) not wired in v1"
        )

    def mouse_up(self, x: int, y: int) -> EnvState:
        raise NotImplementedError(
            "mouse_up: cua-driver press-hold (drag / mouse_button_up) not wired in v1"
        )

    def hover_at(self, x: int, y: int) -> EnvState:
        # NOTE: on macOS/Windows move_cursor moves the agent-cursor OVERLAY, not
        # the real OS pointer, so this will not trigger native :hover behaviour
        # there. It is a real pointer move on Linux.
        self._driver.call("move_cursor", self._win({"x": x, "y": y}))
        return self.current_state()

    def drag_and_drop(
        self, x: int, y: int, destination_x: int, destination_y: int
    ) -> EnvState:
        self._driver.call(
            "drag",
            self._win(
                {
                    "from_x": x,
                    "from_y": y,
                    "to_x": destination_x,
                    "to_y": destination_y,
                }
            ),
        )
        return self.current_state()

    # ── keyboard ─────────────────────────────────────────────────────────────
    def type_text(self, text: str, press_enter: bool = False) -> EnvState:
        # type_text inserts at the focused element's cursor (AX value-set, with
        # a CGEvent fallback). window_id is optional/harmless here.
        self._driver.call("type_text", {"pid": self._pid, "text": text})
        if press_enter:
            self._press_key("enter")
        return self.current_state()

    def type_text_at(
        self, x: int, y: int, text: str, press_enter: bool, clear_before_typing: bool
    ) -> EnvState:
        self._driver.call("click", self._win({"x": x, "y": y}))
        if clear_before_typing:
            # Select-all then delete via the focused element.
            self._press_key("a", modifiers=[self._select_all_modifier()])
            self._press_key("delete")
        self._driver.call("type_text", {"pid": self._pid, "text": text})
        if press_enter:
            self._press_key("enter")
        return self.current_state()

    def key_combination(self, keys: list[str]) -> EnvState:
        # Gemini emits combos like ["control", "c"]. cua-driver's press_key
        # takes ONE key plus a `modifiers` array — split the combo accordingly.
        *modifiers, key = keys if keys else [""]
        self._press_key(key, modifiers=modifiers)
        return self.current_state()

    def press_key(self, key: str) -> EnvState:
        self._press_key(key)
        return self.current_state()

    def _press_key(self, key: str, modifiers: list[str] | None = None) -> None:
        args: dict[str, Any] = {"pid": self._pid, "key": key}
        if modifiers:
            # Normalise common aliases to what cua-driver accepts
            # (cmd/shift/option/alt/ctrl/fn).
            args["modifiers"] = [self._normalize_modifier(m) for m in modifiers]
        self._driver.call("press_key", args)

    @staticmethod
    def _normalize_modifier(m: str) -> str:
        m = m.lower()
        return {
            "control": "ctrl",
            "command": "cmd",
            "super": "cmd",
            "meta": "cmd",
            "win": "cmd",
            "windows": "cmd",
        }.get(m, m)

    @staticmethod
    def _select_all_modifier() -> str:
        # cmd on macOS, ctrl elsewhere. cua-driver maps cmd→the platform's
        # primary modifier on Windows/Linux as well, so cmd is a safe default;
        # callers on Linux/Windows can override via CUA_DRIVER_SELECT_ALL.
        env = os.environ.get("CUA_DRIVER_SELECT_ALL")
        if env:
            return env
        return "cmd" if sys.platform == "darwin" else "ctrl"

    def key_down(self, key: str) -> EnvState:
        raise NotImplementedError(
            "key_down: cua-driver key-hold split not wired in v1 (use key_combination)"
        )

    def key_up(self, key: str) -> EnvState:
        raise NotImplementedError(
            "key_up: cua-driver key-hold split not wired in v1 (use key_combination)"
        )

    # ── scroll ───────────────────────────────────────────────────────────────
    # cua-driver's scroll is KEYSTROKE-based (PageDown/arrows) — it takes a
    # direction + granularity + repeat count, NOT x/y coordinates.
    def scroll_document(
        self, direction: Literal["up", "down", "left", "right"]
    ) -> EnvState:
        self._driver.call(
            "scroll", {"pid": self._pid, "direction": direction, "by": "page", "amount": 1}
        )
        return self.current_state()

    def scroll_at(
        self,
        x: int,
        y: int,
        direction: Literal["up", "down", "left", "right"],
        magnitude: int,
    ) -> EnvState:
        # cua-driver scroll is keystroke-based and has no (x, y) target; the
        # focused region scrolls. `magnitude` maps to keystroke repetitions
        # (clamped to the tool's 1-50 range). (x, y) are accepted but ignored.
        amount = max(1, min(int(magnitude), 50))
        self._driver.call(
            "scroll",
            {"pid": self._pid, "direction": direction, "by": "line", "amount": amount},
        )
        return self.current_state()

    # ── waits ────────────────────────────────────────────────────────────────
    def wait(self, seconds: int) -> EnvState:
        time.sleep(seconds)
        return self.current_state()

    def wait_5_seconds(self) -> EnvState:
        return self.wait(5)

    # ── browser-only verbs: adapted for a desktop env ────────────────────────
    # The Computer interface is browser-shaped. On a native desktop these are
    # no-ops or not meaningful; we keep them so the env satisfies the ABC.
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
