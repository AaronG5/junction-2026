"""CLI: uv run python -m scrapers.telegram [--posts N] [--channels a,b] [--refresh]"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from scrapers.oxylabs import (AuthenticationError, MissingApiKeyError, RateLimitError,
                              WebApiClient)
from scrapers.telegram.fetch import (DEFAULT_POSTS, INPUT, PARSED_DIR, RAW_DIR, REPORT_NAME,
                                     ChannelResult, load_channels, run_channels)

POSTS_ENV = "TELEGRAM_POSTS_PER_CHANNEL"


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def default_posts() -> int:
    value = os.environ.get(POSTS_ENV, "").strip()
    if not value:
        try:
            from local_ai.state import read_env
            value = read_env().get(POSTS_ENV, "").strip()
        except ImportError:
            value = ""
    if not value:
        return DEFAULT_POSTS
    try:
        return _positive_int(value)
    except argparse.ArgumentTypeError as e:
        raise SystemExit(f"{POSTS_ENV}={value!r}: {e}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scrapers.telegram",
        description="Scrape Telegram channel previews through the Oxylabs Web API "
                    "and parse the posts into JSON.")
    parser.add_argument("--posts", type=_positive_int, metavar="N",
                        help=f"posts per channel (default: {POSTS_ENV} from the environment "
                             f"or .env, else {DEFAULT_POSTS})")
    parser.add_argument("--channels", metavar="A,B",
                        help="comma-separated usernames to scrape (default: all in the input)")
    parser.add_argument("--refresh", action="store_true",
                        help="ignore saved raw HTML and scrape again")
    parser.add_argument("--workers", type=_positive_int, default=4, metavar="K",
                        help="channels scraped in parallel (default: 4; lower it on 429s)")
    parser.add_argument("--input", type=Path, default=INPUT, help="channel list JSON")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR, help="where raw HTML is kept")
    parser.add_argument("--out-dir", type=Path, default=PARSED_DIR, help="where parsed JSON goes")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the summary")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    n = args.posts or default_posts()

    channels = load_channels(args.input)
    if args.channels:
        wanted = {c.strip().lower() for c in args.channels.split(",") if c.strip()}
        unknown = wanted - {c.username.lower() for c in channels}
        if unknown:
            parser.error(f"not in {args.input.name}: {', '.join(sorted(unknown))}")
        channels = [c for c in channels if c.username.lower() in wanted]

    try:
        client = WebApiClient()
    except MissingApiKeyError as e:
        print(f"{e}. Add it to .env as OXYLABS_WEB_API_KEY=<your key>.", file=sys.stderr)
        return 2

    width = len(str(len(channels)))

    def progress(done: int, r: ChannelResult) -> None:
        line = (f"[{done:>{width}}/{len(channels)}] {r.channel.username}: "
                f"{len(r.posts)}/{r.requested} posts, {r.status}, region {r.region}")
        if r.reason:
            line += f" ({r.reason})"
        print(line, file=sys.stderr, flush=True)

    print(f"Scraping {len(channels)} channel(s), {n} posts each.", file=sys.stderr)
    try:
        results = run_channels(client, channels, n, raw_root=args.raw_dir,
                               parsed_root=args.out_dir, refresh=args.refresh,
                               workers=args.workers, on_result=progress)
    except (AuthenticationError, RateLimitError) as e:
        print(f"\nStopped: {e}\nFinished channels were saved; "
              f"see {args.out_dir / REPORT_NAME}.", file=sys.stderr)
        return 1

    counts = {s: sum(r.status == s for r in results) for s in ("ok", "partial", "unreadable")}
    ru = [r.channel.username for r in results if r.region != "default"]
    print(f"\nDone: {counts['ok']} ok, {counts['partial']} partial, {counts['unreadable']} unreadable; "
          f"{sum(len(r.posts) for r in results)} posts in {args.out_dir}", file=sys.stderr)
    if ru:
        print(f"Needed {', '.join(sorted({r.region for r in results if r.region != 'default'}))}: "
              f"{', '.join(ru)}", file=sys.stderr)
    for r in results:
        if r.status == "unreadable":
            print(f"Unreadable: {r.channel.username}: {r.reason}", file=sys.stderr)
    print(f"Report: {args.out_dir / REPORT_NAME}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
