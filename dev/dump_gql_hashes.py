"""
Dump the newest Twitch GQL persisted-query hashes into a file.

WHY THIS WORKS THE WAY IT DOES
------------------------------
Twitch does NOT ship the persisted-query hashes as literals in its JS bundles.
The web client computes them at runtime as ``sha256(print(queryDocument))`` (via
SubtleCrypto) and only the resulting hash is sent to the server, inside the POST
body to ``https://gql.twitch.tv/gql``:

    {
        "operationName": "DropCampaignDetails",
        "variables": {...},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": "039277bf..."}}
    }

So the only reliable, low-maintenance way to obtain the *current* hashes is to
read them straight out of real GQL traffic. This script does exactly that, in
two modes:

  1. HAR mode (recommended, no extra dependencies):
       - Open https://www.twitch.tv in your browser, open DevTools -> Network.
       - Filter by "gql" and browse the site (open the Drops inventory, a game
         directory page, a stream, etc.) so the relevant operations fire. Being
         logged in is what lets you capture the authenticated operations
         (Inventory, ViewerDropsDashboard, ...).
       - Right-click the Network panel -> "Save all as HAR with content".
       - Run:  python dev/dump_gql_hashes.py --har path/to/twitch.har

  2. Live mode (optional, needs Playwright):
       - pip install playwright && playwright install chromium
       - Run:  python dev/dump_gql_hashes.py --live
       - A browser window opens; log in and click around to trigger operations,
         then return to the terminal and press Enter to dump what was captured.

The script only DUMPS the captured hashes (to a JSON file) and prints a diff
against the hashes currently hard-coded in constants.py. It never edits
constants.py -- updating it stays a manual, reviewed step, because a rotated
hash sometimes comes with changed variables / response shape that the parsing
code must be checked against by a human.
"""

from __future__ import annotations

import re
import sys
import json
import argparse
from pathlib import Path
from typing import Any, Iterable

GQL_URL_MARKER = "gql.twitch.tv"
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# Matches entries in constants.py's GQL_QUERIES, e.g.:
#     "GetStreamInfo": GQLPersistedQuery(
#         "VideoPlayerStreamInfoOverlayChannel",
#         "198492e0857f6aedead9665c81c5a06d67b25b58034649687124083ff288597d",
# The optional "#..." tolerates an inline comment after GQLPersistedQuery( .
CONSTANTS_RE = re.compile(
    r'"(?P<alias>\w+)"\s*:\s*GQLPersistedQuery\(\s*(?:#.*\n\s*)?'
    r'"(?P<op>[^"]+)"\s*,\s*"(?P<hash>[0-9a-f]{64})"'
)


def extract_from_body(text: str) -> dict[str, str]:
    """Pull ``operationName -> sha256Hash`` pairs out of one GQL POST body.

    Handles both single operations and batched (list) request bodies, and
    silently ignores inline (non-persisted) queries that carry no hash.
    """
    found: dict[str, str] = {}
    try:
        payload: Any = json.loads(text)
    except (ValueError, TypeError):
        return found
    ops: Iterable[Any] = payload if isinstance(payload, list) else [payload]
    for op in ops:
        if not isinstance(op, dict):
            continue
        name = op.get("operationName")
        try:
            sha = op["extensions"]["persistedQuery"]["sha256Hash"]
        except (KeyError, TypeError):
            continue
        if isinstance(name, str) and isinstance(sha, str) and HASH_RE.match(sha):
            found[name] = sha
    return found


def collect_from_har(paths: list[Path]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in paths:
        with path.open("r", encoding="utf8") as file:
            har = json.load(file)
        entries = har.get("log", {}).get("entries", [])
        gql_entries = 0
        for entry in entries:
            request = entry.get("request", {})
            if GQL_URL_MARKER not in request.get("url", ""):
                continue
            gql_entries += 1
            body = (request.get("postData") or {}).get("text", "")
            hashes.update(extract_from_body(body))
        print(f"  {path.name}: {gql_entries} GQL request(s) scanned")
    return hashes


def collect_live() -> dict[str, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit(
            "Live mode needs Playwright. Install it with:\n"
            "    pip install playwright\n"
            "    playwright install chromium"
        )
    hashes: dict[str, str] = {}

    def on_request(request: Any) -> None:
        if GQL_URL_MARKER in request.url and request.method == "POST":
            body = request.post_data
            if body:
                hashes.update(extract_from_body(body))

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context()
        context.on("request", on_request)
        page = context.new_page()
        page.goto("https://www.twitch.tv/drops/inventory")
        print(
            "\nBrowser opened. Log in and click around (Drops inventory, a game\n"
            "directory page, a live stream...) to trigger GQL operations.\n"
            "When done, come back here and press Enter to dump."
        )
        input()
        browser.close()
    return hashes


def load_current(constants_path: Path) -> dict[str, tuple[str, str]]:
    """Return ``operationName -> (alias, current_hash)`` from constants.py."""
    text = constants_path.read_text(encoding="utf8")
    current: dict[str, tuple[str, str]] = {}
    for m in CONSTANTS_RE.finditer(text):
        current[m.group("op")] = (m.group("alias"), m.group("hash"))
    return current


def print_diff(captured: dict[str, str], current: dict[str, tuple[str, str]]) -> None:
    print("\n=== Comparison against constants.py ===")
    print(f"{'operationName':<42} {'alias':<22} status")
    print("-" * 78)
    for op, (alias, old_hash) in sorted(current.items()):
        new_hash = captured.get(op)
        if new_hash is None:
            status = "not captured"
        elif new_hash == old_hash:
            status = "same"
        else:
            status = f"CHANGED -> {new_hash}"
        print(f"{op:<42} {alias:<22} {status}")
    extra = sorted(set(captured) - set(current))
    if extra:
        print("\nCaptured operations not used by this app (informational):")
        for op in extra:
            print(f"  {op} = {captured[op]}")


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--har", nargs="+", type=Path, metavar="FILE", help="one or more HAR exports to parse")
    source.add_argument("--live", action="store_true", help="capture live via Playwright")
    parser.add_argument("--out", type=Path, default=Path("gql_hashes.json"), help="output file (default: gql_hashes.json)")
    parser.add_argument("--constants", type=Path, default=repo_root / "constants.py", help="path to constants.py for the diff")
    args = parser.parse_args()

    if args.live:
        print("Capturing live GQL traffic via Playwright...")
        captured = collect_live()
    else:
        print(f"Parsing {len(args.har)} HAR file(s)...")
        captured = collect_from_har(args.har)

    if not captured:
        sys.exit(
            "No persisted-query hashes found. Make sure GQL requests actually fired "
            "(filter Network by 'gql' and exercise the relevant pages while logged in)."
        )

    args.out.write_text(json.dumps(dict(sorted(captured.items())), indent=4) + "\n", encoding="utf8")
    print(f"\nDumped {len(captured)} operation hash(es) -> {args.out}")

    if args.constants.exists():
        print_diff(captured, load_current(args.constants))
    else:
        print(f"(skipped diff: {args.constants} not found)")


if __name__ == "__main__":
    main()
