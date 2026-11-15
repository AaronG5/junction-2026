"""CLI: uv run python -m scrapers.youtube [--videos N] [--days D | --since DATE] [--channels a,b]

How far back each channel goes:
    (nothing)          the N newest videos with a transcript, N from YOUTUBE_VIDEOS_PER_CHANNEL or 10
    --videos N         the N newest videos with a transcript
    --days 30          every video from the last 30 days
    --since 2026-10-01 every video since that day (UTC)
    --days 30 --videos 100   both: stop at whichever comes first

Run it again to pick up new videos: listings are re-checked once they're 6 hours old
(--recheck-hours), and nothing already saved is fetched again. --update re-lists every
channel now, --cached makes no requests and rebuilds posts.json, --refresh starts over.
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
from scrapers.youtube.fetch import (DEFAULT_MAX_VIDEOS, DEFAULT_RECHECK_HOURS, DEFAULT_VIDEOS,
                                    INPUT, PARSED_DIR, POSTS_NAME, RAW_DIR, REPORT_NAME,
                                    ChannelResult, load_sources, run_channels)
from scrapers.youtube.parse import to_utc_iso

VIDEOS_ENV = "YOUTUBE_VIDEOS_PER_CHANNEL"


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
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


def default_videos() -> int:
    value = os.environ.get(VIDEOS_ENV, "").strip()
    if not value:
        try:
            from local_ai.state import read_env
            value = read_env().get(VIDEOS_ENV, "").strip()
        except ImportError:
            value = ""
    if not value:
        return DEFAULT_VIDEOS
    try:
        return _positive_int(value)
    except argparse.ArgumentTypeError as e:
        raise SystemExit(f"{VIDEOS_ENV}={value!r}: {e}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scrapers.youtube",
        description="Collect the transcripts of YouTube channels' videos through the Oxylabs "
                    "Web API and parse them into JSON.")
    parser.add_argument("--videos", type=_positive_int, metavar="N",
                        help=f"the N newest videos with a transcript per channel (default without "
                             f"--days/--since: {VIDEOS_ENV} from the environment or .env, else "
                             f"{DEFAULT_VIDEOS}). With --days/--since it caps the count instead.")
    when = parser.add_mutually_exclusive_group()
    when.add_argument("--days", type=_positive_int, metavar="D",
                      help="every video from the last D days")
    when.add_argument("--since", type=parse_since, metavar="DATE",
                      help="every video since DATE (2026-10-01, UTC; compared by day)")
    parser.add_argument("--max-videos", type=_positive_int, default=DEFAULT_MAX_VIDEOS, metavar="M",
                        help=f"the largest channel listing to ask for (default: {DEFAULT_MAX_VIDEOS})")
    parser.add_argument("--recheck-hours", type=_positive_float, default=DEFAULT_RECHECK_HOURS,
                        metavar="H", help=f"re-list a channel once its listing is H hours old "
                                          f"(default: {DEFAULT_RECHECK_HOURS:g})")
    parser.add_argument("--channels", metavar="A,B",
                        help="comma-separated keys (handles without @) to scrape (default: all)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--update", action="store_true", help="re-list every channel now")
    mode.add_argument("--cached", action="store_true",
                      help="make no requests: rebuild posts.json from the saved responses")
    mode.add_argument("--refresh", action="store_true",
                      help="throw away the channels' saved responses and posts and start over")
    parser.add_argument("--retry-missing", action="store_true",
                        help="also retry videos remembered as having no transcript or as unavailable")
    parser.add_argument("--parallel", type=_positive_int, default=3, metavar="P",
                        help="channels at a time (default: 3; lower it on 429s)")
    parser.add_argument("--workers", type=_positive_int, default=4, metavar="K",
                        help="requests at a time per channel (default: 4; lower it on 429s)")
    parser.add_argument("--input", type=Path, default=INPUT, help="channel list JSON")
    parser.add_argument("--raw-dir", type=Path, default=RAW_DIR, help="where raw responses are kept")
    parser.add_argument("--out-dir", type=Path, default=PARSED_DIR,
                        help=f"where {POSTS_NAME} and {REPORT_NAME} go")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the summary")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    since = args.since
    if args.days:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
    n = args.videos if (args.videos or since) else default_videos()

    sources = load_sources(args.input)
    if args.channels:
        wanted = {c.strip().lstrip("@").lower() for c in args.channels.split(",") if c.strip()}
        unknown = wanted - {s.key for s in sources}
        if unknown:
            parser.error(f"not in {args.input.name}: {', '.join(sorted(unknown))}")
        sources = [s for s in sources if s.key in wanted]

    client = None
    if not args.cached:
        try:
            client = WebApiClient()
        except MissingApiKeyError as e:
            print(f"{e}. Add it to .env as OXYLABS_WEB_API_KEY=<your key>.", file=sys.stderr)
            return 2

    width = len(str(len(sources)))

    def progress(done: int, r: ChannelResult) -> None:
        wanted = f"/{r.requested}" if r.requested else ""
        line = (f"[{done:>{width}}/{len(sources)}] {r.source.key}: {r.got}{wanted} videos, "
                f"{r.status}, {r.scraped} requests")
        if r.reason:
            line += f" ({r.reason})"
        print(line, file=sys.stderr, flush=True)

    scope = (f"every video since {to_utc_iso(since)[:10]}" if since and not n else
             f"up to {n} videos per channel since {to_utc_iso(since)[:10]}" if since else
             f"{n} videos per channel")
    how = (", re-listing every channel" if args.update else ", no requests" if args.cached else
           ", from scratch" if args.refresh else "")
    print(f"Scraping {len(sources)} channel(s): {scope}{how}.", file=sys.stderr)
    try:
        results = run_channels(client, sources, n, since=since, update=args.update,
                               refresh=args.refresh, cached=args.cached,
                               retry_missing=args.retry_missing, recheck_hours=args.recheck_hours,
                               max_videos=args.max_videos, parallel=args.parallel,
                               workers=args.workers, raw_root=args.raw_dir,
                               parsed_root=args.out_dir, on_result=progress)
    except (AuthenticationError, RateLimitError) as e:
        print(f"\nStopped: {e}\nFinished channels were saved to {args.out_dir / POSTS_NAME}; "
              f"see {args.out_dir / REPORT_NAME}.", file=sys.stderr)
        return 1

    counts = {s: sum(r.status == s for r in results) for s in ("ok", "partial", "unreadable")}
    print(f"\nDone: {counts['ok']} ok, {counts['partial']} partial, {counts['unreadable']} unreadable; "
          f"{sum(r.added for r in results)} new videos, {sum(r.total for r in results)} in "
          f"{args.out_dir / POSTS_NAME}; {sum(r.scraped for r in results)} requests", file=sys.stderr)
    for r in results:
        if r.status != "ok":
            print(f"{r.status.capitalize()}: {r.source.key}: {r.reason}", file=sys.stderr)
    print(f"Report: {args.out_dir / REPORT_NAME}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
