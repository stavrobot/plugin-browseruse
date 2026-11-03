#!/usr/bin/env -S uv run
# /// script
# dependencies = ["playwright==1.64.1"]
# ///

"""Drive a remote Browser Use Cloud browser through Playwright."""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# Start the clock before importing Playwright so its import time is included.
PROCESS_START = time.monotonic()

from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

API_BASE_URL = "https://api.browser-use.com/api/v4"

DEADLINE_SECONDS = 25.0
STOP_RESERVE_SECONDS = 3.0
CREATE_TIMEOUT_SECONDS = 8.0
NAVIGATION_TIMEOUT_SECONDS = 15.0
ACTION_TIMEOUT_SECONDS = 5.0
STOP_TIMEOUT_SECONDS = 2.5
MAX_TEXT_CHARACTERS = 20_000

KNOWN_PARAMETERS = {"url", "actions", "output"}
OUTPUT_FORMATS = {"text", "html", "screenshot"}
ACTION_FIELDS = {
    "click": ("selector",),
    "fill": ("selector", "value"),
    "press": ("selector", "key"),
    "wait_for": ("selector",),
    "evaluate": ("expression",),
}


class ToolError(Exception):
    """An error that should be reported to the caller without a traceback."""


class DeadlineExceeded(ToolError):
    """The global deadline ran out before a step could complete."""


def time_left() -> float:
    """Return seconds available before the stop reserve, possibly negative."""
    return DEADLINE_SECONDS - STOP_RESERVE_SECONDS - (time.monotonic() - PROCESS_START)


def step_timeout(seconds: float, context: str) -> float:
    """Return the timeout for a step, limited by the remaining global budget."""
    remaining = time_left()
    if remaining <= 0:
        raise DeadlineExceeded(f"Deadline exceeded before {context}.")
    return min(seconds, remaining)


def step_timeout_ms(seconds: float, context: str) -> int:
    """Return a Playwright timeout in milliseconds, never zero (zero disables it)."""
    return max(1, int(step_timeout(seconds, context) * 1000))


def redact(text: str, secret: str) -> str:
    """Remove a secret from text that is about to be logged."""
    return text.replace(secret, "[redacted]") if secret else text


def api_call(
    method: str,
    path: str,
    api_key: str,
    payload: dict | None,
    *,
    timeout: float,
    expected_status: int,
) -> dict:
    """Call the Browser Use REST API and return the decoded JSON response."""
    headers = {"X-Browser-Use-API-Key": api_key}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        f"{API_BASE_URL}{path}",
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        detail = redact(error.read().decode("utf-8", errors="replace"), api_key)
        raise ToolError(
            f"Browser Use API {method} {path} returned HTTP {error.code}: {detail}"
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise ToolError(f"Browser Use API {method} {path} failed: {reason}") from None

    if status != expected_status:
        raise ToolError(
            "Browser Use API "
            f"{method} {path} returned unexpected HTTP {status}: {redact(body, api_key)}"
        )

    try:
        result = json.loads(body)
    except json.JSONDecodeError:
        raise ToolError(
            "Browser Use API "
            f"{method} {path} returned invalid JSON: {redact(body[:500], api_key)}"
        ) from None

    if not isinstance(result, dict):
        raise ToolError(f"Browser Use API {method} {path} returned an unexpected response.")
    return result


def create_browser(api_key: str, proxy_country_code: str | None) -> dict:
    """Create a short-lived browser and return its API response."""
    timeout = step_timeout(CREATE_TIMEOUT_SECONDS, "creating the browser")
    return api_call(
        "POST",
        "/browsers",
        api_key,
        {
            "timeout": 1,
            "proxyCountryCode": proxy_country_code,
            "solveCaptchas": False,
            "enableRecording": False,
        },
        timeout=timeout,
        expected_status=201,
    )


def stop_browser(api_key: str, browser_id: str) -> None:
    """Stop a browser, logging but swallowing failures so they do not mask results."""
    try:
        api_call(
            "PATCH",
            f"/browsers/{browser_id}",
            api_key,
            {"action": "stop"},
            timeout=STOP_TIMEOUT_SECONDS,
            expected_status=200,
        )
    except Exception as error:  # noqa: BLE001 - the original result or error must win.
        print(
            f"[stavrobot] browseruse could not stop browser {browser_id}: {redact(str(error), api_key)}",
            file=sys.stderr,
        )


def evaluate_expression(expression: str, timeout_ms: int) -> str:
    """Wrap a JavaScript expression with a timeout so evaluate respects its budget."""
    return (
        "Promise.race(["
        f"Promise.resolve().then(() => ({expression})),"
        "new Promise((_, reject) => setTimeout("
        f"() => reject(new Error('Evaluation timed out')), {timeout_ms}))"
        "])"
    )


def validate_actions(actions: list) -> None:
    """Validate the nested action objects, which the plugin runner does not check."""
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            raise ToolError(f"Action {index} must be an object.")

        name = action.get("action")
        if not isinstance(name, str) or name not in ACTION_FIELDS:
            expected = ", ".join(sorted(ACTION_FIELDS))
            raise ToolError(
                f"Action {index} has unknown action {name!r}; expected one of: {expected}."
            )

        for field in ACTION_FIELDS[name]:
            value = action.get(field)
            if not isinstance(value, str):
                raise ToolError(
                    f"Action {index} ({name}) requires the string field '{field}'."
                )
            if field != "value" and not value.strip():
                raise ToolError(
                    f"Action {index} ({name}) requires a non-empty '{field}'."
                )


def read_parameters() -> dict:
    """Read and validate the tool parameters from stdin."""
    try:
        parameters = json.load(sys.stdin)
    except json.JSONDecodeError as error:
        raise ToolError(
            f"Invalid JSON on stdin: {error.msg} at line {error.lineno} column {error.colno}."
        ) from None

    if not isinstance(parameters, dict):
        raise ToolError("Parameters must be a JSON object.")

    unknown = sorted(set(parameters) - KNOWN_PARAMETERS)
    if unknown:
        raise ToolError(f"Unknown parameter(s): {', '.join(unknown)}.")

    url = parameters.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ToolError("Parameter 'url' is required and must be a non-empty string.")

    output = parameters.get("output", "text")
    if not isinstance(output, str) or output not in OUTPUT_FORMATS:
        raise ToolError("Parameter 'output' must be one of: text, html, screenshot.")

    actions = parameters.get("actions", [])
    if not isinstance(actions, list):
        raise ToolError("Parameter 'actions' must be an array.")
    validate_actions(actions)

    return {"url": url, "actions": actions, "output": output}


def read_config() -> dict:
    """Read the plugin configuration without ever exposing its contents in errors."""
    path = Path("../config.json")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ToolError(f"Could not read {path}: {error.strerror or error}.") from None

    try:
        config = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ToolError(
            f"Invalid JSON in {path}: {error.msg} at line {error.lineno}."
        ) from None

    if not isinstance(config, dict):
        raise ToolError(f"{path} must contain a JSON object.")
    return config


def output_file_path(suffix: str) -> tuple[Path, str]:
    """Return a destination inside STAVROBOT_TEMP_DIR and its filename."""
    temp_dir = os.environ.get("STAVROBOT_TEMP_DIR")
    if not temp_dir:
        raise ToolError("STAVROBOT_TEMP_DIR is not set; cannot write the output file.")

    directory = Path(temp_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ToolError(f"Could not create the output directory: {error}.") from None

    filename = f"{uuid.uuid4().hex}.{suffix}"
    return directory / filename, filename


def navigate(page, url: str) -> None:
    """Navigate to the requested URL within the step budget."""
    timeout = step_timeout_ms(NAVIGATION_TIMEOUT_SECONDS, "loading the page")
    try:
        page.goto(url, timeout=timeout)
    except PlaywrightTimeoutError:
        if time_left() <= 0:
            raise DeadlineExceeded("Deadline exceeded while loading the page.") from None
        raise ToolError(f"Timed out after {timeout / 1000:.1f}s loading {url}.") from None
    except PlaywrightError as error:
        raise ToolError(f"Could not load {url}: {error}") from None


def read_page_text(page) -> str:
    """Read the visible body text within the step budget."""
    timeout = step_timeout_ms(ACTION_TIMEOUT_SECONDS, "reading the page text")
    try:
        return page.locator("body").inner_text(timeout=timeout)
    except PlaywrightTimeoutError:
        if time_left() <= 0:
            raise DeadlineExceeded("Deadline exceeded while reading the page text.") from None
        raise ToolError(
            f"Timed out after {timeout / 1000:.1f}s reading the page text."
        ) from None
    except PlaywrightError as error:
        raise ToolError(f"Could not read the page text: {error}") from None


def perform_actions(page, actions: list) -> list:
    """Run the actions in order and return the results of evaluate steps."""
    evaluations = []

    for index, action in enumerate(actions):
        name = action["action"]
        context = f"action {index} ({name})"
        try:
            timeout = step_timeout_ms(ACTION_TIMEOUT_SECONDS, context)
            if name == "click":
                page.locator(action["selector"]).click(timeout=timeout)
            elif name == "fill":
                page.locator(action["selector"]).fill(action["value"], timeout=timeout)
            elif name == "press":
                page.locator(action["selector"]).press(action["key"], timeout=timeout)
            elif name == "wait_for":
                page.locator(action["selector"]).wait_for(state="visible", timeout=timeout)
            else:
                value = page.evaluate(
                    evaluate_expression(action["expression"], timeout)
                )
                try:
                    json.dumps(value, allow_nan=False)
                except (TypeError, ValueError):
                    raise ToolError(
                        f"Action {index} (evaluate) returned a value that is not JSON-serialisable."
                    ) from None
                evaluations.append(value)
        except DeadlineExceeded:
            raise
        except PlaywrightTimeoutError:
            if time_left() <= 0:
                raise DeadlineExceeded(
                    f"Deadline exceeded during action {index} ({name})."
                ) from None
            raise ToolError(f"Action {index} ({name}) timed out.") from None
        except PlaywrightError as error:
            if time_left() <= 0:
                raise DeadlineExceeded(
                    f"Deadline exceeded during action {index} ({name})."
                ) from None
            raise ToolError(f"Action {index} ({name}) failed: {error}") from None

    return evaluations


def drive(cdp_url: str, parameters: dict) -> dict:
    """Connect to the browser, perform the work, and build the tool result."""
    with sync_playwright() as playwright:
        connect_timeout = step_timeout_ms(
            NAVIGATION_TIMEOUT_SECONDS, "connecting to the browser"
        )
        browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=connect_timeout)
        try:
            try:
                page = browser.contexts[0].pages[0]
            except IndexError:
                raise ToolError(
                    "The remote browser did not expose an existing page."
                ) from None

            navigate(page, parameters["url"])
            evaluations = perform_actions(page, parameters["actions"])

            result = {"url": page.url, "title": page.title()}
            if evaluations:
                result["evaluations"] = evaluations

            if parameters["output"] == "text":
                text = read_page_text(page)
                result["text"] = text[:MAX_TEXT_CHARACTERS]
                result["truncated"] = len(text) > MAX_TEXT_CHARACTERS
            elif parameters["output"] == "html":
                step_timeout(ACTION_TIMEOUT_SECONDS, "reading the page HTML")
                path, filename = output_file_path("html")
                try:
                    path.write_text(page.content(), encoding="utf-8")
                except OSError as error:
                    raise ToolError(f"Could not write the HTML file: {error}.") from None
                result["file"] = filename
            else:
                timeout = step_timeout_ms(
                    NAVIGATION_TIMEOUT_SECONDS, "taking the screenshot"
                )
                path, filename = output_file_path("png")
                try:
                    page.screenshot(path=str(path), full_page=True, timeout=timeout)
                except PlaywrightTimeoutError:
                    if time_left() <= 0:
                        raise DeadlineExceeded(
                            "Deadline exceeded while taking the screenshot."
                        ) from None
                    raise ToolError("Timed out while taking the screenshot.") from None
                except PlaywrightError as error:
                    raise ToolError(f"Could not take a screenshot: {error}") from None
                result["file"] = filename

            return result
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001 - cleanup must not hide the result.
                pass


def run(parameters: dict, config: dict) -> dict:
    """Create a browser, run the request, and always stop the browser afterwards."""
    api_key = config.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ToolError("Configuration value 'api_key' is missing or empty.")
    api_key = api_key.strip()
    # A key with whitespace or control characters makes urllib raise errors that
    # contain an escaped copy of the key, which plain redaction cannot catch.
    if not api_key.isprintable() or any(character.isspace() for character in api_key):
        raise ToolError("Configuration value 'api_key' contains invalid characters.")

    proxy_country_code = config.get("proxy_country_code")
    if proxy_country_code is not None:
        if not isinstance(proxy_country_code, str):
            raise ToolError("Configuration value 'proxy_country_code' must be a string.")
        proxy_country_code = proxy_country_code.strip() or None

    session = create_browser(api_key, proxy_country_code)
    browser_id = session.get("id")
    if not isinstance(browser_id, str) or not browser_id:
        raise ToolError("Browser Use did not return a browser ID.")

    stop_lock = threading.Lock()
    stop_state = {"stopped": False}

    def stop_once() -> None:
        """Stop the browser if the watchdog has not already claimed it."""
        with stop_lock:
            if stop_state["stopped"]:
                return
            stop_state["stopped"] = True
            stop_browser(api_key, browser_id)

    def watchdog_fire() -> None:
        """Stop the browser and exit if the main thread is stuck past the deadline."""
        with stop_lock:
            if stop_state["stopped"]:
                return
            stop_state["stopped"] = True
            try:
                print(
                    "Error: Deadline exceeded before browser automation completed.",
                    file=sys.stderr,
                    flush=True,
                )
                stop_browser(api_key, browser_id)
            finally:
                sys.stderr.flush()
                os._exit(1)

    watchdog = threading.Timer(max(0.0, time_left()), watchdog_fire)
    watchdog.daemon = True

    try:
        watchdog.start()
        print(f"[stavrobot] browseruse created browser {browser_id}", file=sys.stderr)

        cdp_url = session.get("cdpUrl")
        if not isinstance(cdp_url, str) or not cdp_url:
            raise ToolError("Browser Use did not return a CDP URL.")

        return drive(cdp_url, parameters)
    finally:
        watchdog.cancel()
        stop_once()


def main() -> None:
    """Read the request, run it, and report errors clearly on stderr."""
    api_key = ""

    try:
        parameters = read_parameters()
        config = read_config()
        raw_api_key = config.get("api_key")
        api_key = raw_api_key.strip() if isinstance(raw_api_key, str) else ""
        result = run(parameters, config)
        json.dump(result, sys.stdout, allow_nan=False)
    except DeadlineExceeded as error:
        print(f"Error: {redact(str(error), api_key)}", file=sys.stderr)
        sys.exit(1)
    except ToolError as error:
        print(f"Error: {redact(str(error), api_key)}", file=sys.stderr)
        sys.exit(1)
    except PlaywrightError as error:
        print(
            f"Error: {redact(f'browser automation failed: {error}', api_key)}",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as error:  # noqa: BLE001 - keep the plugin output clean.
        print(
            f"Error: {redact(f'unexpected failure: {error}', api_key)}",
            file=sys.stderr,
        )
        sys.exit(1)


main()
