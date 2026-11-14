"""Launcher for the self-hosted setup. Run via launch.bat / launch.sh, or:

    python -m local_ai [options]

First run: detects hardware, picks and downloads a Gemma model, installs
llama.cpp + llama-swap, starts MySQL/Qdrant and the AI server, then prepares
the database and vector search, builds the dashboard and starts it at
http://127.0.0.1:8000. Later runs skip everything already done (and work
offline once installed).

To add a step: write a function taking the Context and append it to STEPS.
"""
import argparse
import sys
from dataclasses import dataclass, field

if sys.version_info < (3, 10):
    sys.exit("Python 3.10 or newer is required. Download it from https://www.python.org/downloads/")

from . import backend, download, hardware, paths, runtime, selector, services, swap_config, ui
from .state import ensure_env, load_state, read_env, save_state

GEMMA_TERMS = "https://ai.google.dev/gemma/terms"


@dataclass
class Context:
    args: argparse.Namespace
    state: dict
    catalog: dict = None
    hw: hardware.Hardware = None
    chat_model: dict = None
    placement: str = ""
    chat_path: object = None
    mmproj_path: object = None
    embed_path: object = None
    server: object = None
    backend: str = ""
    swap_binary: object = None
    config_hash: str = ""
    results: dict = field(default_factory=dict)


def model_dir(model):
    return paths.MODELS_DIR / model["file"].removesuffix(".gguf")


def is_downloaded(model):
    d = model_dir(model)
    files = [model["file"]] + ([model["mmproj"]["file"]] if model.get("mmproj") else [])
    return all(download.is_verified(d / f) for f in files)


# ---- steps --------------------------------------------------------------------

def step_hardware(ctx):
    paths.ensure_dirs()
    ensure_env()
    ctx.catalog = selector.load_catalog()
    ctx.hw = hardware.detect(force_cpu=ctx.args.cpu, disk_path=str(paths.MODELS_DIR))
    for line in hardware.describe(ctx.hw):
        ui.info(line)
    # The dashboard's System page shows this (backend/veritas/system.py reads the state file).
    ctx.state["hardware"] = backend.hardware_summary(ctx.hw)
    save_state(ctx.state)


def _label(c, recommended=False):
    m = c.model
    gb = selector.download_gb(m)
    have = "already downloaded" if is_downloaded(m) else f"{gb:.1f} GB download"
    vision = ", reads images" if m.get("mmproj") else ""
    rec = "  <- recommended" if recommended else ""
    return f"{m['id']:<20} {have}, runs on {c.placement}{vision}{rec}"


def step_select_model(ctx):
    catalog, hw, state = ctx.catalog, ctx.hw, ctx.state
    downloaded = [m["id"] for m in catalog["chat_models"] if is_downloaded(m)]
    ranked = selector.rank(hw, catalog, downloaded)
    by_id = {c.model["id"]: c for c in ranked}

    if ctx.args.model:
        chosen = selector.find_model(catalog, ctx.args.model)
        if not chosen:
            raise ui.StepFailed(f"Unknown model '{ctx.args.model}'. See: --list-models")
        cand = by_id[chosen["id"]]
        if not cand.fits:
            ui.warn(f"{chosen['id']} may not run well here ({cand.placement}); using it as requested.")
    elif state.get("chat_model") in by_id and not ctx.args.reselect \
            and state.get("backend") == hw.backend:
        cand = by_id[state["chat_model"]]
        ui.ok(f"Using {cand.model['id']} (chosen earlier; --reselect to choose again)")
    else:
        fitting = [c for c in ranked if c.fits]
        if not fitting:
            raise ui.StepFailed("No model fits this computer (not enough memory or disk space). "
                                "Use the cloud version instead.")
        # Offer the recommendation plus a few lighter, faster alternatives.
        options = fitting[:1] + sorted(fitting[1:], key=lambda c: -c.model["quality"])[:4]
        ui.info("Which AI model should be installed?")
        idx = ui.ask_choice("Choose a number", [_label(c, i == 0) for i, c in enumerate(options)])
        cand = options[idx]

    ctx.chat_model = cand.model
    # A forced, non-fitting model still needs a usable placement label.
    ctx.placement = cand.placement if cand.fits else ("cpu" if hw.backend == "cpu" else "gpu+cpu")
    state.update(chat_model=cand.model["id"], backend=hw.backend,
                 upgrade=backend.upgrade_for(ranked, cand.model["id"], selector.download_gb))
    save_state(state)
    ui.ok(f"Model: {cand.model['id']} ({ctx.placement})")


def step_download_chat(ctx):
    m = ctx.chat_model
    if m.get("license") == "gemma" and not is_downloaded(m):
        ui.info(f"This model is provided under the Gemma Terms of Use: {GEMMA_TERMS}")
    d = model_dir(m)
    ctx.chat_path = download.fetch_hf(m["repo"], m["file"], d, f"{m['id']} model")
    ctx.mmproj_path = None
    if m.get("mmproj"):
        ctx.mmproj_path = download.fetch_hf(m["repo"], m["mmproj"]["file"], d,
                                            f"{m['id']} image support")


def step_download_embedding(ctx):
    e = ctx.catalog["embedding_model"]
    d = paths.EMBEDDINGS_DIR / e["file"].removesuffix(".gguf")
    ctx.embed_path = download.fetch_hf(e["repo"], e["file"], d, f"{e['id']} (text embeddings)")


def step_install_runtime(ctx):
    bundled = ctx.args.bundled_runtime
    ctx.server, ctx.backend = runtime.install_llama_cpp(ctx.hw, ctx.args.build_from_source, bundled)
    ui.ok(f"llama.cpp ready ({ctx.backend}): {ctx.server}")
    ctx.swap_binary = runtime.install_llama_swap(ctx.hw, bundled)
    ui.ok(f"llama-swap ready: {ctx.swap_binary}")


def step_swap_config(ctx):
    text = swap_config.build(ctx.server, ctx.backend, ctx.chat_model, ctx.chat_path,
                             ctx.mmproj_path, ctx.placement, ctx.catalog["embedding_model"],
                             ctx.embed_path, read_env())
    ctx.config_hash = swap_config.write(text)
    ui.ok(f"Wrote {paths.SWAP_CONFIG.relative_to(paths.ROOT)}")


def step_docker(ctx):
    if ctx.args.skip_docker:
        ui.info("Skipped (--skip-docker)")
        return
    ctx.results["databases"] = services.start_docker_services()


def step_ai_server(ctx):
    if ctx.args.no_ai_server:
        ui.info("Skipped (--no-ai-server)")
        return
    if not services.start_llama_swap(ctx.swap_binary, ctx.config_hash):
        raise ui.StepFailed("The AI server could not be started.")
    ui.info("Loading the models into memory (first time can take a few minutes)...")
    if services.wait_models_loaded():
        ui.ok("Models loaded and ready")
        ctx.results["ai"] = True
    else:
        ui.warn(f"Models are still loading or failed; see {paths.LOGS_DIR / 'llama-swap.log'} "
                f"and the llama-swap page at {services.swap_url()}/ui")


def step_database(ctx):
    for line in backend.migrate().splitlines():
        ui.info(line.strip())


def step_vectors(ctx):
    for line in backend.setup_vectors().splitlines()[-1:]:
        ui.info(line.strip())


def step_frontend(ctx):
    result = backend.build_frontend()
    if result == "current":
        ui.ok("The dashboard is up to date")
    elif result == "built":
        ui.ok("Dashboard built")
    elif result == "prebuilt":
        ui.warn("Node.js isn't installed, so the dashboard wasn't rebuilt; using the existing build.")
    else:
        ui.warn("Node.js isn't installed and the dashboard has never been built. "
                "Install Node.js (https://nodejs.org) and run the launcher again.")


def step_demo(ctx):
    if not ctx.args.demo_data:
        ui.info("Skipped (add --demo-data to load example data)")
        return
    ui.ok(backend.load_demo_data())


def step_backend(ctx):
    if ctx.args.no_backend:
        ui.info("Skipped (--no-backend)")
        return
    backend.start_backend()
    ctx.results["dashboard"] = backend.backend_url()


STEPS = [
    ("Checking this computer", step_hardware),
    ("Choosing the AI model", step_select_model),
    ("Downloading the AI model", step_download_chat),
    ("Downloading the embedding model", step_download_embedding),
    ("Installing the AI engine (llama.cpp + llama-swap)", step_install_runtime),
    ("Configuring the AI engine", step_swap_config),
    ("Starting the databases (MySQL + Qdrant)", step_docker),
    ("Starting the AI server", step_ai_server),
    ("Preparing the database", step_database),
    ("Setting up vector search", step_vectors),
    ("Building the dashboard", step_frontend),
    ("Loading the demo data", step_demo),
    ("Starting the dashboard", step_backend),
]


# ---- commands -------------------------------------------------------------------

def cmd_list_models(args):
    hw = hardware.detect(force_cpu=args.cpu, disk_path=str(paths.ROOT))
    catalog = selector.load_catalog()
    downloaded = [m["id"] for m in catalog["chat_models"] if is_downloaded(m)]
    print(f"Backend: {hw.backend}.  Models (best fit first):")
    for c in selector.rank(hw, catalog, downloaded):
        mark = "ok " if c.fits else "-- "
        print(f"  {mark}{_label(c)}" if c.fits else f"  {mark}{c.model['id']:<20} {c.placement}")


def cmd_status(_args):
    env = read_env()
    state = load_state()
    print(f"Model:      {state.get('chat_model', 'not chosen yet')}")
    print(f"AI server:  {'running at ' + services.swap_url(env) if services.llama_swap_running() else 'stopped'}")
    for port_key, name in (("MYSQL_PORT", "MySQL"), ("QDRANT_PORT", "Qdrant")):
        port = env.get(port_key)
        up = port and not services.port_free(port)
        print(f"{name + ':':<11} {'listening on 127.0.0.1:' + port if up else 'stopped'}")
    print(f"Dashboard:  {'running at ' + backend.backend_url(env) if backend.backend_running(env) else 'stopped'}")


def cmd_stop(args):
    if backend.stop_backend():
        print("Dashboard stopped.")
    services.stop_llama_swap()
    print("AI server stopped.")
    if not args.skip_docker:
        services.stop_docker_services()
        print("Databases stopped.")


def main():
    p = argparse.ArgumentParser(prog="launch", description="Set up and start the self-hosted AI.")
    p.add_argument("--model", help="use this model instead of the automatic choice (see --list-models)")
    p.add_argument("--reselect", action="store_true", help="choose the model again")
    p.add_argument("--list-models", action="store_true", help="show which models fit this computer")
    p.add_argument("--cpu", action="store_true", help="do not use the GPU")
    p.add_argument("--build-from-source", action="store_true",
                   help="compile llama.cpp instead of downloading the prebuilt release")
    p.add_argument("--bundled-runtime", action="store_true",
                   help="ignore llama.cpp/llama-swap already on this computer; use the pinned versions")
    p.add_argument("--skip-docker", action="store_true", help="do not start MySQL/Qdrant")
    p.add_argument("--no-ai-server", action="store_true", help="set up everything but do not start llama-swap")
    p.add_argument("--no-backend", action="store_true", help="do not start the dashboard server")
    p.add_argument("--demo-data", action="store_true",
                   help="load the example data (only into an empty database; remove it with backend/demo_data.py --remove)")
    p.add_argument("--status", action="store_true", help="show what is running")
    p.add_argument("--stop", action="store_true", help="stop the dashboard, the AI server and the databases")
    p.add_argument("-y", "--yes", action="store_true", help="accept the defaults, never ask")
    args = p.parse_args()
    ui.ASSUME_YES = args.yes

    if args.list_models:
        return cmd_list_models(args)
    if args.status:
        return cmd_status(args)
    if args.stop:
        return cmd_stop(args)

    ctx = Context(args=args, state=load_state())
    try:
        for i, (title, fn) in enumerate(STEPS, 1):
            ui.step(i, len(STEPS), title)
            fn(ctx)
    except ui.StepFailed as e:
        ui.err(str(e))
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Run the launcher again to continue where it left off.")
        return 130

    print()
    if ctx.results.get("ai") and ctx.results.get("dashboard"):
        print(f"All set. Open the dashboard: {ctx.results['dashboard']}")
    elif ctx.results.get("dashboard"):
        print(f"The dashboard is at {ctx.results['dashboard']}, but the AI isn't ready yet (see above).")
    else:
        print("Setup finished with warnings (see above).")
    print("Run the launcher again any time; use --stop to shut everything down.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
