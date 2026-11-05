# plugin-browseruse

A Stavrobot plugin that opens web pages in a remote browser hosted by [Browser Use Cloud](https://browser-use.com). It can click, type and run JavaScript on a page, then return the page text, its HTML or a screenshot. Use it for pages that need JavaScript or interaction. The browser runs at Browser Use, so nothing is installed locally.

## Before you install

You need a Browser Use Cloud account with credits and an API key. Create both at [cloud.browser-use.com](https://cloud.browser-use.com). Billing is pay as you go, from $0.02 per browser-hour, with a one-minute minimum for each browser.

The plugin keeps costs low without any action from you:

- Each call creates a new browser and stops it when the call ends, also when the call fails.
- Each browser has a one-minute timeout, so it stops by itself even if the stop request fails.
- By default the browser uses a direct connection ($0.20/GB), not the residential proxy ($5/GB).

## Installation

Tell Stavrobot to install `https://github.com/stavrobot/plugin-browseruse`.

## Configuration

- **`api_key`** (required): your Browser Use API key, from the API keys page of [cloud.browser-use.com](https://cloud.browser-use.com).
- **`proxy_country_code`** (optional): a two-letter country code, for example `us`, to send traffic through a Browser Use residential proxy in that country. Use it for sites that block direct traffic. Leave it empty for a direct connection.

## Tools

### `browse`

Opens a URL, runs an optional list of actions in order, and returns the final URL and page title.

- `url`: the page to open.
- `actions` (optional): a list of steps. Each step is `click`, `fill`, `press`, `wait_for` or `evaluate`, with a selector, a value, a key or a JavaScript expression. The results of `evaluate` steps are returned.
- `output` (optional): `text` (the default) returns the visible text, up to 20,000 characters. `html` returns the page HTML as a file. `screenshot` returns a full-page PNG.

Each call must finish in about 50 seconds, and the result arrives as a follow-up message. The browser does not stay open between calls, so put all the steps for one task in one call.
