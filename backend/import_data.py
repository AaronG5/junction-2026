"""Load the team's collected data into the database:  uv run python backend/import_data.py

  (no option)          load every source in data/parsed/ (Telegram posts and the news articles),
                       then make a vector for every post that has none yet
  --source telegram    only this source (telegram, respublika, bukime_vieningi); repeatable
  --no-embed           rows only, no vectors (the AI server isn't needed)
  --embed-only         skip the files; only make the missing vectors
  --limit N            at most N posts per source and N vectors (a quick check)
  --batch N            texts per embedding request (default 32)
  --vectors FILE       precomputed vectors (.npz); default data/vectors/posts_768.npz when present.
                       Get it with:  uv run python backend/fetch_vectors.py   (minutes instead of hours)

Safe to run again: posts already stored with the same text are skipped, and a stopped
embedding run continues where it left off. Needs MySQL and Qdrant; the local AI
(llama-swap) as well unless --no-embed.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from veritas import importer  # noqa: E402
from veritas.ai import AiError, AiUnavailable, LocalAI  # noqa: E402
from veritas.config import Settings  # noqa: E402
from veritas.db import DatabaseUnavailable, connection  # noqa: E402
from veritas.db.migrations import pending  # noqa: E402
from veritas.vectors import Qdrant, VectorStoreError, VectorStoreUnavailable  # noqa: E402

# A whole file is loaded in one connection; big files take minutes.
READ_TIMEOUT = 3600


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # Lithuanian and Russian text on a cp1252 pipe must not crash the import
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Load the collected posts and articles into Veritas Lens.")
    parser.add_argument("--source", action="append", choices=sorted(importer.SOURCES), metavar="NAME",
                        help=f"only this source; one of {', '.join(importer.SOURCES)} (repeatable)")
    parser.add_argument("--no-embed", action="store_true", help="store the rows only; make no vectors")
    parser.add_argument("--embed-only", action="store_true", help="read no files; only make the missing vectors")
    parser.add_argument("--limit", type=int, help="at most this many posts per source and vectors")
    parser.add_argument("--batch", type=int, default=importer.EMBED_BATCH, help="texts per embedding request")
    parser.add_argument("--vectors", type=Path, default=None, help="precomputed vectors (.npz); default data/vectors/posts_768.npz")
    args = parser.parse_args(argv)
    if args.no_embed and args.embed_only:
        print("Choose one of --no-embed and --embed-only.", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit < 1 or args.batch < 1:
        print("--limit and --batch must be at least 1.", file=sys.stderr)
        return 2
    names = [] if args.embed_only else (args.source or list(importer.SOURCES))

    say = lambda message: print(message, flush=True)  # noqa: E731
    try:
        settings = Settings.load()
        with connection(settings, read_timeout=READ_TIMEOUT) as conn:
            if pending(conn):
                print("The database schema isn't up to date. Run first:  uv run python backend/migrate.py", file=sys.stderr)
                return 1
            if args.no_embed:
                result = importer.run_import(conn, names, embed=False, limit=args.limit, progress=say)
            else:
                pre = importer.load_precomputed(args.vectors)
                if pre:
                    say(f"Using {len(pre)} precomputed vectors.")
                elif args.vectors is not None:
                    print(f"No vectors file at {args.vectors}.", file=sys.stderr)
                    return 1
                with Qdrant.from_settings(settings) as qdrant, LocalAI.from_settings(settings, timeout=300) as ai:
                    try:
                        ai.check()
                    except AiUnavailable:
                        if not pre:
                            raise
                        say("The local AI isn't running: storing the precomputed vectors only.")
                        ai = None
                    result = importer.run_import(conn, names, qdrant=qdrant, ai=ai, batch=args.batch,
                                                 limit=args.limit, progress=say, precomputed=pre)
    except (importer.DataFileError, DatabaseUnavailable, VectorStoreUnavailable, VectorStoreError,
            AiUnavailable, AiError, ValueError) as exc:
        print(f"Import stopped. {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nImport stopped. Run it again to continue where it left off.", file=sys.stderr)
        return 130

    loaded = sum(c.total for c in result["sources"].values())
    print(f"Done: {loaded} posts loaded, {result['embedded']} vectors made"
          + (f", {result['pending']} posts still without a vector." if result["pending"] else "."))
    if result["pending"] and args.no_embed:
        print("Make the vectors with:  uv run python backend/import_data.py --embed-only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
