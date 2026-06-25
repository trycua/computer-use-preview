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
import argparse
import os

from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file

from agent import BrowserAgent
from computers import BrowserbaseComputer, DesktopComputer, PlaywrightComputer


PLAYWRIGHT_SCREEN_SIZE = (1440, 900)
# Default coordinate space for the desktop env before the target window is
# resolved on enter (DesktopComputer overrides this with the real window
# screenshot size).
DESKTOP_SCREEN_SIZE = (1280, 800)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the browser agent with a query.")
    parser.add_argument(
        "--query",
        type=str,
        required=True,
        help="The query for the browser agent to execute.",
    )

    parser.add_argument(
        "--env",
        type=str,
        choices=("playwright", "browserbase", "desktop"),
        default="playwright",
        help="The computer use environment to use. 'desktop' drives a native "
        "app via Cua Driver (https://github.com/trycua/cua).",
    )
    parser.add_argument(
        "--app",
        type=str,
        default=None,
        help="[--env desktop] Target app display name to launch/attach "
        "(e.g. \"KiCad\", \"Calculator\"). On Windows you may also pass an AUMID.",
    )
    parser.add_argument(
        "--bundle-id",
        dest="bundle_id",
        type=str,
        default=None,
        help="[--env desktop] App bundle id (macOS, e.g. com.apple.calculator) "
        "or AUMID (Windows). Preferred over --app when set.",
    )
    parser.add_argument(
        "--window-title",
        dest="window_title",
        type=str,
        default=None,
        help="[--env desktop] Substring of the target window title, to "
        "disambiguate when the app has multiple windows.",
    )
    parser.add_argument(
        "--initial_url",
        type=str,
        default="https://www.google.com",
        help="The inital URL loaded for the computer.",
    )
    parser.add_argument(
        "--highlight_mouse",
        action="store_true",
        default=False,
        help="If possible, highlight the location of the mouse.",
    )
    parser.add_argument(
        "--model",
        default='gemini-3.5-flash',
        help="Set which main model to use.",
    )
    args = parser.parse_args()

    if args.env == "playwright":
        env = PlaywrightComputer(
            screen_size=PLAYWRIGHT_SCREEN_SIZE,
            initial_url=args.initial_url,
            highlight_mouse=args.highlight_mouse,
        )
    elif args.env == "browserbase":
        env = BrowserbaseComputer(
            screen_size=PLAYWRIGHT_SCREEN_SIZE,
            initial_url=args.initial_url
        )
    elif args.env == "desktop":
        if not (args.app or args.bundle_id):
            raise ValueError(
                "--env desktop requires --app <name> (or --bundle-id <id>) to "
                "name the native app to drive."
            )
        env = DesktopComputer(
            screen_size=DESKTOP_SCREEN_SIZE,
            app=args.app,
            bundle_id=args.bundle_id,
            window_title=args.window_title,
        )
    else:
        raise ValueError("Unknown environment: ", args.env)

    with env as browser_computer:
        agent = BrowserAgent(
            browser_computer=browser_computer,
            query=args.query,
            model_name=args.model,
        )
        agent.agent_loop()
    return 0


if __name__ == "__main__":
    main()
