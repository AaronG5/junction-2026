"""CLI: uv run python -m scrapers.telegram [--posts N] [--days D | --since DATE] [--channels a,b]

How far back each channel goes:
    (nothing)          the N newest posts, N from TELEGRAM_POSTS_PER_CHANNEL or 20
    --posts N          the N newest posts
    --days 30          every post from the last 30 days
    --since 2026-10-01 every post since that date (UTC)
    --days 30 --posts 500   both: stop at whichever comes first

Saved pages are reused. Add --update to also fetch posts published since the
last run (one request per channel when little is new), or --refresh to start over.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scrapers.oxylabs import (AuthenticationError, MissingApiKeyError, RateLimitError,
                              WebApiClient)
from scrapers.telegram.fetch import (DEFAULT_MAX_PAGES, DEFAULT_POSTS, INPUT, PARSED_DIR,
                                     POSTS_NAME, RAW_DIR, REPORT_NAME, ChannelResult,
                                     load_channels, run_channels)
from scrapers.telegram.parse import to_utc_iso

POSTS_ENV = "TELEGRAM_POSTS_PER_CHANNEL"


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def parse_since(value: str) -> datetime:
    """'2026-10-01' or '2026-10-01T12:00' (UTC unless an offset is given) -> aware datetime."""
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a date like 2026-10-01 or 2026-10-01T12:00") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt > datetime.now(timezone.utc):
        raise argparse.ArgumentTypeError(f"{value!r} is in the future")
    return dt


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
                        help=f"the N newest posts per channel (default without --days/--since: "
                             f"{POSTS_ENV} from the environment or .env, else {DEFAULT_POSTS}). "
                             "With --days/--since it caps the count instead.")
    when = parser.add_mutually_exclusive_group()
    when.add_argument("--days", type=_positive_int, metavar="D",
                      help="every post from the last D days")
    when.add_argument("--since", type=parse_since, metavar="DATE",
                      help="every post since DATE (2026-10-01 or 2026-10-01T12:00, UTC)")
    parser.add_argument("--max-pages", type=_positive_int, default=DEFAULT_MAX_PAGES, metavar="P",
                        help=f"safety cap on requests per channel in one run, about 20 posts each "
                             f"(default: {DEFAULT_MAX_PAGES}). A later run continues where it stopped.")
    parser.add_argument("--channels", metavar="A,B",
                        help="comma-separated usernames to scrape (default: all in the input)")
    fresh = parser.add_mutually_exclusive_group()
    fresh.add_argument("--update", action="store_true",
                       help="also fetch posts published since the saved pages, keeping the rest")
    fresh.add_argument("--refresh", action="store_true",
                       help="throw away saved raw HTML and scrape everything again")
    parser.add_argument("--workers", type=_positive_int, default=4, metavar="K",
                        help="channels scraped in parallel (default: 4; lower it on 429s)")
    parser.add_argument("--input", type=Path, default=INPUT, help="channel list JSON")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR, help="where raw HTML is kept")
    parser.add_argument("--out-dir", type=Path, default=PARSED_DIR,
                        help=f"where {POSTS_NAME} and {REPORT_NAME} go")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the summary")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    since = args.since
    if args.days:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
    n = args.posts if (args.posts or since) else default_posts()

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
        wanted = f"/{r.requested}" if r.requested else ""
        line = (f"[{done:>{width}}/{len(channels)}] {r.channel.username}: "
                f"{len(r.posts)}{wanted} posts, {r.status}, region {r.region}")
        if r.reason:
            line += f" ({r.reason})"
        print(line, file=sys.stderr, flush=True)

    scope = (f"every post since {to_utc_iso(since)}" if since and not n else
             f"up to {n} posts per channel since {to_utc_iso(since)}" if since else
             f"{n} posts per channel")
    mode = ", updating saved pages" if args.update else ", from scratch" if args.refresh else ""
    print(f"Scraping {len(channels)} channel(s): {scope}{mode}.", file=sys.stderr)
    try:
        results = run_channels(client, channels, n, since=since, update=args.update,
                               raw_root=args.raw_dir, parsed_root=args.out_dir, refresh=args.refresh,
                               max_pages=args.max_pages, workers=args.workers,
                               on_result=progress)
    except (AuthenticationError, RateLimitError) as e:
        print(f"\nStopped: {e}\nFinished channels were saved to {args.out_dir / POSTS_NAME}; "
              f"see {args.out_dir / REPORT_NAME}.", file=sys.stderr)
        return 1

    counts = {s: sum(r.status == s for r in results) for s in ("ok", "partial", "unreadable")}
    ru = [r.channel.username for r in results if r.region != "default"]
    print(f"\nDone: {counts['ok']} ok, {counts['partial']} partial, {counts['unreadable']} unreadable; "
          f"{sum(len(r.posts) for r in results)} posts in {args.out_dir / POSTS_NAME}", file=sys.stderr)
    if ru:
        print(f"Needed {', '.join(sorted({r.region for r in results if r.region != 'default'}))}: "
              f"{', '.join(ru)}", file=sys.stderr)
    for r in results:
        if r.status != "ok":
            print(f"{r.status.capitalize()}: {r.channel.username}: {r.reason}", file=sys.stderr)
    print(f"Report: {args.out_dir / REPORT_NAME}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
