#!/usr/bin/env python3
"""Pinterest Trends: DISABLED placeholder. Nothing here can reach Pinterest.

This file exists so the switch-on point is obvious and so tests can prove it stays inert. It has no network
code, imports no HTTP library and reads no credential. Read docs/pinterest-trends.md for what has to happen
first (a Pinterest developer app approved by the captain, an OAuth token, the Developer and API Terms read by
a human, and the open terms questions answered). Enabling it is a deliberate, reviewed change, not a flag.

Never add a scraper, a browser path, a third-party Pinterest MCP or any storage of Pinterest data here.
"""
from __future__ import annotations

import sys

ENABLED = False
REASON = ("Pinterest Trends is not switched on. It needs an approved Pinterest developer app and a human reading "
          "the Pinterest Developer and API Terms first. See docs/pinterest-trends.md.")


class PinterestTrendsDisabled(RuntimeError):
    pass


def fetch_trends(*_args, **_kwargs):
    """Always raises. There is no implementation behind it."""
    raise PinterestTrendsDisabled(REASON)


def main(argv=None) -> int:
    print(REASON, file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
