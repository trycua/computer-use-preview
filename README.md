# Computer Use Preview

## Quick Start

This section will guide you through setting up and running Gemini Computer Use, using either the Gemini Developer API or Vertex AI. Follow these steps to get started.

### 1. Installation

**Clone the Repository**

```bash
git clone https://github.com/google-gemini/computer-use-preview.git
cd computer-use-preview
```

**Set up Python Virtual Environment and Install Dependencies**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Install Playwright and Browser Dependencies**

```bash
# Install system dependencies required by Playwright for Chrome
playwright install-deps chrome

# Install the Chrome browser for Playwright
playwright install chrome
```

### 2. Configuration
You can get started using either the Gemini Developer API or Vertex AI.

#### A. If using the Gemini Developer API:

You need a Gemini API key to use the agent:

```bash
export GEMINI_API_KEY="YOUR_GEMINI_API_KEY"
```

Or to add this to your virtual environment:

```bash
echo 'export GEMINI_API_KEY="YOUR_GEMINI_API_KEY"' >> .venv/bin/activate
# After editing, you'll need to deactivate and reactivate your virtual
# environment if it's already active:
deactivate
source .venv/bin/activate
```

Replace `YOUR_GEMINI_API_KEY` with your actual key.

#### B. If using the Vertex AI Client:

You need to explicitly use Vertex AI, then provide project and location to use the agent:

```bash
export USE_VERTEXAI=true
export VERTEXAI_PROJECT="YOUR_PROJECT_ID"
export VERTEXAI_LOCATION="YOUR_LOCATION"
```

Or to add this to your virtual environment:

```bash
echo 'export USE_VERTEXAI=true' >> .venv/bin/activate
echo 'export VERTEXAI_PROJECT="your-project-id"' >> .venv/bin/activate
echo 'export VERTEXAI_LOCATION="your-location"' >> .venv/bin/activate
# After editing, you'll need to deactivate and reactivate your virtual
# environment if it's already active:
deactivate
source .venv/bin/activate
```

Replace `YOUR_PROJECT_ID` and `YOUR_LOCATION` with your actual project and location.

#### C. Using a `.env` file (Recommended for local development):

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

Then open `.env` and replace the placeholder values with your actual API keys. The project will automatically load this file on startup — no need to manually export environment variables each session.

### 3. Running the Tool

The primary way to use the tool is via the `main.py` script.

**General Command Structure:**

```bash
python main.py --query "Go to Google and type 'Hello World' into the search bar"
```

**Available Environments:**

You can specify a particular environment with the ```--env <environment>``` flag.  Available options:

- `playwright`: Runs the browser locally using Playwright.
- `browserbase`: Connects to a Browserbase instance.
- `desktop`: Drives a **native desktop application** (Windows / macOS / Linux) via [Cua Driver](https://github.com/trycua/cua). See ["Desktop (native apps via Cua Driver)"](#desktop-native-apps-via-cua-driver) below.

**Local Playwright**

Runs the agent using a Chrome browser instance controlled locally by Playwright.

```bash
python main.py --query="Go to Google and type 'Hello World' into the search bar" --env="playwright"
```

You can also specify an initial URL for the Playwright environment:

```bash
python main.py --query="Go to Google and type 'Hello World' into the search bar" --env="playwright" --initial_url="https://www.google.com/search?q=latest+AI+news"
```

**Browserbase**

Runs the agent using Browserbase as the browser backend. Ensure the proper Browserbase environment variables are set:`BROWSERBASE_API_KEY` and `BROWSERBASE_PROJECT_ID`.

```bash
python main.py --query="Go to Google and type 'Hello World' into the search bar" --env="browserbase"
```

### Desktop (native apps via Cua Driver)

The `playwright` and `browserbase` environments drive a **browser viewport**. The
`desktop` environment instead drives a **single native application window** on a
real macOS / Windows / Linux host, using [Cua Driver](https://github.com/trycua/cua)
— a background computer-use daemon. The *same* Gemini built-in computer tool
(`screenshot` / `click` / `type` / `scroll` / `key`) is mapped onto Cua Driver
tool calls scoped to the window you target.

**With vs. without Cua Driver**

| | Without Cua Driver (browser envs) | With Cua Driver (`--env desktop`) |
|---|---|---|
| Target surface | A browser page (Playwright / Browserbase) | Any **native** GUI app — KiCad, CAD tools, legacy desktop apps |
| Scope | The whole browser viewport | One `window_id`, even when it is backgrounded / minimized / on another Space |
| Focus behavior | Foreground browser tab | **No focus-steal, no real-mouse move** — input is posted to the target window in the background |
| Coordinate frame | Page/viewport screenshot | The target window's screenshot (window-local pixels) |
| Precision on tiny targets | Limited by viewport resolution | Cua Driver's `zoom` can crop a region and round-trip clicks for pixel-accurate grounding on small controls (see "Open items" — not yet wired into this backend) |

One-line framing: *without* Cua Driver, Gemini drives a browser viewport in the
foreground; *with* Cua Driver, the same model drives a single native window in the
background.

**Install Cua Driver** (it is a native binary, **not** a pip package):

```bash
# macOS / Linux: one-line installer
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"
cua-driver --version
```

On macOS, start the daemon and grant permissions once (Accessibility + Screen
Recording):

```bash
open -n -g -a CuaDriver --args serve
cua-driver permissions status
```

Windows installation and the full tool reference live in the
[Cua Driver docs](https://github.com/trycua/cua). If the binary is not on your
`PATH`, point this backend at it with `CUA_DRIVER_BIN=/path/to/cua-driver`.

**Run Gemini against a native window:**

```bash
export GEMINI_API_KEY="YOUR_GEMINI_API_KEY"

# Launch/attach to an app by display name and drive it:
python main.py --env desktop --app "Calculator" \
  --query "Compute 12.5% of 880 and read back the result"

# A KiCad example mirroring Cua's benchmark walkthrough:
python main.py --env desktop --app "KiCad" \
  --query "In the open schematic, set R3 to the nearest E96 value for a 2x gain"
```

You can also target by bundle id / AUMID (`--bundle-id com.apple.calculator`)
and disambiguate multi-window apps with `--window-title "<substring>"`.

**Open items** (this is a reference integration; these are honest gaps):

- **`zoom` / `from_zoom` coordinate round-trip is not wired here.** Cua Driver's
  pixel-accurate-on-zoom path requires the bridge to track "is the model
  currently looking at a zoom crop?" and set `from_zoom=true` on the resulting
  click. This backend always feeds the model the full-window screenshot, so
  clicks are full-window-local. Wiring zoom is the next step for tiny-target
  precision.
- **`mouse_down` / `mouse_up` and `key_down` / `key_up`** raise
  `NotImplementedError` — Cua Driver models press-hold via its `drag` gesture
  rather than a standalone down/up split.
- **`hover_at`** uses Cua Driver's `move_cursor`, which on macOS/Windows moves an
  agent-cursor *overlay* rather than the real OS pointer, so it will not trigger
  native `:hover` styling there (it is a real pointer move on Linux).
- **Native popup/`select` menus** close when their window is backgrounded; a
  purely pixel-driven loop won't know to use the accessibility `set_value` path
  Cua Driver recommends for those.

The model loop has been validated end-to-end at the Cua-Bench level (see Cua's
KiCad evaluation of Gemini 3.5 Flash); this backend wires the same driver tools
into this repo's `Computer` interface.

**Available Models:**

You can choose the model to use by specifying the ```--model <model name>``` flag. Available options on Gemini Developer API and Vertex AI Client:

- `gemini-3.5-flash`: This is the default model.
- `gemini-2.5-computer-use-preview-10-2025`: An earlier computer use preview model.
- `gemini-3-flash-preview`: The preview version of Gemini 3 Flash.

## Agent CLI

The `main.py` script is the command-line interface (CLI) for running the browser agent.

### Command-Line Arguments

| Argument | Description | Required | Default | Supported Environment(s) |
|-|-|-|-|-|
| `--query` | The natural language query for the browser agent to execute. | Yes | N/A | All |
| `--env` | The computer use environment to use. Must be one of the following: `playwright`, `browserbase`, or `desktop` | No | `playwright` | All |
| `--initial_url` | The initial URL to load when the browser starts. | No | https://www.google.com | `playwright`, `browserbase` |
| `--highlight_mouse` | If specified, the agent will attempt to highlight the mouse cursor's position in the screenshots. This is useful for visual debugging. | No | False (not highlighted) | `playwright` |
| `--app` | Target native app display name to launch/attach (e.g. `"KiCad"`). On Windows you may also pass an AUMID. Required for `desktop` unless `--bundle-id` is given. | No | N/A | `desktop` |
| `--bundle-id` | App bundle id (macOS, e.g. `com.apple.calculator`) or AUMID (Windows). Preferred over `--app` when set. | No | N/A | `desktop` |
| `--window-title` | Substring of the target window title, to disambiguate when the app has multiple windows. | No | N/A | `desktop` |
| `--model` | The model to use. See the "Available Models" section for more information. | No | `gemini-3.5-flash` | All |

### Environment Variables

| Variable | Description | Required |
|-|-|-|
| GEMINI_API_KEY | Your API key for the Gemini model. | Yes |
| BROWSERBASE_API_KEY | Your API key for Browserbase. | Yes (when using the browserbase environment) |
| BROWSERBASE_PROJECT_ID | Your Project ID for Browserbase. | Yes (when using the browserbase environment) |
| CUA_DRIVER_BIN | Path to the `cua-driver` binary, if it is not on your `PATH`. | No (only for the `desktop` environment) |

## Known Issues

### Playwright Dropdown Menu

On certain operating systems, the Playwright browser is unable to capture `<select>` elements because they are rendered by the operating system. As a result, the agent is unable to send the correct screenshot to the model.

There are several ways to mitigate this.

1. Use the Browserbase option instead of Playwright.
2. Inject a script like [proxy-select](https://github.com/amitamb/proxy-select) to render a custom `<select>` element. You must inject `proxy-select.css` and `proxy-select.js` into each page that has a non-custom `<select>` element. You can do this in the [`Playwright.__enter__`](https://github.com/google-gemini/computer-use-preview/blob/main/computers/playwright/playwright.py#L100) method by adding a few lines of code, like the following (replacing `PROXY_SELECT_JS` and `PROXY_SELECT_CSS` with the appropriate variables):

```python
self._page.add_init_script(PROXY_SELECT_JS)
def inject_style(page):
    try:
        page.add_style_tag(content=PROXY_SELECT_CSS)
    except Exception as e:
        print(f"Error injecting style: {e}")

self._page.on('domcontentloaded', inject_style)
```

Note, option 2 does not work 100% of the time, but is a temporary workaround for certain websites. The better option is to use Browserbase.
