"""Group the imported posts into narratives, with a preflight check and live progress. Run on the server:

  uv run --extra clustering python backend/cluster_run.py            # first run groups every post
  uv run --extra clustering python backend/cluster_run.py --cloud    # names + insights by Gemma 26B on Modal (much faster)
  uv run --extra clustering python backend/cluster_run.py --no-read  # grouping + names only, skip the slow insights
  uv run --extra clustering python backend/cluster_run.py --rebuild  # from scratch
  add --fast to run UMAP on every CPU core (several times faster on a multi-core machine)

`--extra clustering` installs umap-learn + scikit-learn on the fly (the usual reason grouping "breaks").
Every step prints a timestamped line, and a heartbeat line every 15 s shows it is still working.
"""
import argparse
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

CLOUD_CHAT_URL = "https://augustasrink--ppg-gemma-chat-serve.us-east.modal.run/v1"
CLOUD_MODEL = "gemma-4-26B-A4B-it"

T0 = time.time()
_last = {"msg": "starting", "at": time.time(), "n": 0}


def say(msg: str) -> None:
    _last.update(msg=msg, at=time.time(), n=_last["n"] + 1)
    print(f"[{datetime.now():%H:%M:%S} +{_elapsed()}] {msg}", flush=True)


def _elapsed(since: float = None) -> str:
    s = int(time.time() - (since or T0))
    return f"{s // 60:02d}:{s % 60:02d}"


def heartbeat(stop: threading.Event) -> None:
    while not stop.wait(15):
        print(f"[{datetime.now():%H:%M:%S} +{_elapsed()}]   ... still working ({_elapsed(_last['at'])} on: {_last['msg'][:90]})",
              flush=True)


def preflight(settings, cloud: bool) -> bool:
    ok = True
    say("PREFLIGHT 1/4  clustering packages (umap-learn, scikit-learn)")
    try:
        import umap  # noqa: F401
        from sklearn.cluster import HDBSCAN  # noqa: F401
        say("  OK")
    except ImportError as exc:
        say(f"  MISSING ({exc}). Run with:  uv run --extra clustering python backend/cluster_run.py")
        ok = False
    say("PREFLIGHT 2/4  MySQL")
    from veritas.db import connection
    try:
        with connection(settings) as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n, SUM(embedded_at IS NOT NULL) AS e FROM posts WHERE is_stub = FALSE AND deleted_at IS NULL")
            r = cur.fetchone()
            say(f"  OK: {int(r['n'] or 0)} posts, {int(r['e'] or 0)} with a vector")
            if not r["e"]:
                say("  No vectors yet: run  uv run python backend/fetch_vectors.py  then  uv run python backend/import_data.py")
                ok = False
    except Exception as exc:
        say(f"  FAILED: {exc}")
        ok = False
    say("PREFLIGHT 3/4  Qdrant")
    from veritas.vectors import Qdrant
    try:
        with Qdrant.from_settings(settings) as q:
            n = q.count("posts")
            say(f"  OK: {n} post vectors in Qdrant")
            if n < 50:
                say("  Too few vectors to group (need at least ~50).")
                ok = False
    except Exception as exc:
        say(f"  FAILED: {exc}")
        ok = False
    say("PREFLIGHT 4/4  Gemma for names" + (" (cloud, Modal)" if cloud else " (local llama-swap)") + ", 20 s max")
    box = {}

    def probe():
        try:
            ai = make_ai(settings, cloud)
            ai.check()
            ai.close()
            box["ok"] = True
        except Exception as exc:  # noqa: BLE001
            box["err"] = exc
    t = threading.Thread(target=probe, daemon=True)
    t.start()
    t.join(20)
    if box.get("ok"):
        say("  OK")
    elif "err" in box:
        say(f"  FAILED: {box['err']}" + ("" if cloud else "  (try --cloud, or start the local AI with the launcher)"))
        ok = False
    else:
        say("  No answer within 20 s (model still loading / cold start). Continuing: Gemma is only needed after grouping.")
    return ok


def make_ai(settings, cloud: bool):
    from veritas.ai import LocalAI
    if cloud:
        base = f"http://127.0.0.1:{settings.llama_swap_port}/v1"
        return LocalAI.cloud(base, CLOUD_CHAT_URL, CLOUD_MODEL)
    return LocalAI.from_settings(settings, timeout=300)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(description="Group posts into narratives, with progress.")
    p.add_argument("--rebuild", action="store_true", help="group everything again from scratch")
    p.add_argument("--days", type=int, default=None, help="with --rebuild: only the last N days")
    p.add_argument("--no-read", action="store_true", help="skip reading typical posts (the slow insight step)")
    p.add_argument("--cloud", action="store_true", help="use Gemma 26B on Modal for names and insights")
    p.add_argument("--fast", action="store_true", help="UMAP on every CPU core (faster; not bit-for-bit reproducible)")
    p.add_argument("--skip-preflight", action="store_true")
    args = p.parse_args()

    import os
    os.environ["CLUSTER_PROGRESS"] = "1"          # phase markers + UMAP epoch progress inside the grouping step
    if args.fast:
        os.environ["CLUSTER_FAST"] = "1"
    from veritas.config import Settings
    settings = Settings.load()
    if not args.skip_preflight and not preflight(settings, args.cloud):
        say("Preflight failed: fix the lines marked MISSING/FAILED above and run again.")
        return 1

    from veritas.clustering_job import cluster_and_read, summary
    from veritas.db import connection
    from veritas.factcheck import gemini_for
    from veritas.vectors import Qdrant

    stop = threading.Event()
    threading.Thread(target=heartbeat, args=(stop,), daemon=True).start()
    say("STEP 1  grouping (UMAP + HDBSCAN on CPU; a few minutes for ~50k posts, longer on 2 vCPU)")
    try:
        with connection(settings, read_timeout=3600) as conn, Qdrant.from_settings(settings) as qdrant, \
                make_ai(settings, args.cloud) as ai:
            gemini = gemini_for(conn, settings)
            say("  claims + fact checks: " + (f"Gemini ({gemini.model})" if gemini else "Gemma, no fact checks (no GEMINI_API_KEY)"))
            result = cluster_and_read(conn, qdrant, ai, mode="rebuild" if args.rebuild else "update", days=args.days,
                                      say=say, read=not args.no_read, gemini=gemini)
    except KeyboardInterrupt:
        stop.set()
        say("Stopped. Run again: finished steps are kept and it continues.")
        return 130
    except Exception as exc:
        stop.set()
        say(f"FAILED: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 1
    stop.set()
    status, message = summary(result)
    say(f"DONE in {_elapsed()}: {message}")
    for c in result.get("clusters", [])[:40]:
        if "name" in c:
            print(f"   {c['size']:>6}  {c['name']}", flush=True)
    return 0 if status in ("done", "up_to_date") else 1


if __name__ == "__main__":
    sys.exit(main())
