#!/usr/bin/env -S uv run
# /// script
# dependencies = ["playwright==1.64.1"]
# ///

"""Populate the uv cache with Playwright so the first browse call is fast."""


def main() -> None:
    """Import Playwright to install its wheel and driver into the uv cache."""
    import playwright  # noqa: F401

    print("Playwright is cached and ready.")


main()
