# junction-2026
Junction 2026: European Tech Renaissance project for Synthetic Logic LAB team

## Self-hosted setup

Everything runs on your own computer: the AI model, the search index and the database.

**Start it**

- Windows: double-click `launch.bat`
- Linux / macOS: run `./launch.sh`

That's it. The first start checks your computer, picks the Gemma model that fits it best, downloads it, and starts everything - the AI models, MySQL and Qdrant, the dashboard build, the server that serves it and the nginx proxy that puts it on the default web port. If the download is interrupted, start it again and it continues where it stopped. Later starts take seconds and work offline.

**You need:** [uv](https://docs.astral.sh/uv/) (the launcher offers to install it; uv brings its own Python) and [Docker Desktop](https://docs.docker.com/get-started/get-docker/) for the databases and the web proxy.

Developers: run Python through uv as well, e.g. `uv run python -m local_ai --status`.

**Useful options** (`launch.bat <option>` or `./launch.sh <option>`):

| Option | What it does |
|---|---|
| `--list-models` | Show which models fit your computer |
| `--reselect` | Choose the model again |
| `--model gemma-4-E4B-it` | Use a specific model |
| `--cpu` | Don't use the graphics card |
| `--status` | Show what is running |
| `--stop` | Stop everything |
| `--host 0.0.0.0` | Serve the dashboard and the AI to the network (what `./launch.sh` does by default) |
| `--local` | The opposite: this computer only |
| `--no-proxy` | Don't serve the dashboard on port 80; leave it on 8000 |
| `--expose-databases` | Publish MySQL and Qdrant on that address too |
| `--demo-data` | Load the example data (only into an empty database) |
| `--import-data` | Load the collected data (see below) and make its vectors |

**The collected data**

`data/parsed/` holds what the team's scrapers collected: about 47,000 posts from 70 Telegram channels (`telegram/posts.json`), and the articles of `respublika.lt` and `bukimevieningi.lt`. It is loaded only on request:

```
./launch.sh --import-data                          # as part of a normal start
uv run python backend/import_data.py               # by hand; --source telegram, --no-embed, --embed-only, --limit N
```

The import stores the posts and their accounts in MySQL and makes a vector for each in Qdrant with the local embedding model. The first run embeds everything, which takes a while; it commits as it goes, so stopping it and running it again continues where it left off. Running it again after a new scrape adds only what is new. Once the vectors are there, "Analyze a link" finds copies and translations among the collected posts, and `uv run python backend/cluster.py` groups them into narratives.

**Where it listens**

`./launch.sh` serves the dashboard on `0.0.0.0:8000`, so a server install is reachable from the web. `launch.bat` and `./launch.sh --local` keep everything on `127.0.0.1` (this computer only). The choice is saved as `BIND_HOST` in `.env` - the backend server and Docker both read it - so later starts keep it, and editing it there works too.

An nginx container (`docker/docker-compose.yml`, config in `docker/nginx/default.conf.template`) sits in front of it and sends everything that arrives on the default web port to port 8000, so the dashboard opens at `http://<this computer>/` with no port number - `:8000` keeps working as well. It starts last, after the dashboard server it routes to, and is skipped - with a message, never fatally - if port 80 is taken or Docker isn't there. `HTTP_PORT` in `.env` moves it to another port, `--no-proxy` leaves it out. The AI server on 8080 is deliberately not proxied: it has no login, so the default web port must not lead to it.

MySQL and Qdrant stay on `127.0.0.1` either way: nothing outside the machine needs them. `--expose-databases` publishes them on `BIND_HOST` as well.

The dashboard has no login of its own, so on a server open to the internet put it behind a reverse proxy that adds HTTPS and a password, or a firewall that only lets you in. `ALLOWED_HOSTS=veritas.example` in `.env` limits which host names the backend answers to; without it a networked server answers to any.

The AI is served at port 8080 (`/v1`) as an OpenAI-compatible API, with two models: `chat` and `embedding`, on the same address as the dashboard. Settings live in `.env` (created on first start).

## Frontend (developers)

The dashboard and landing page live in `frontend/` (React + Vite + TypeScript). Requires Node.js 20+.

```
cd frontend
npm install
npm run dev:demo   # example data, no backend needed -> http://localhost:5173
npm run dev        # real mode: talks to the backend at /api (port 8000)
npm test           # all tests
npm run build      # production build into frontend/dist
npm run test:build # proves demo data never ships in a normal build
```

- The text box on the start page and on Analyze runs the narrative check: free text goes to `POST /api/ext/batch` (then `GET /api/ext/result` is polled until the Gemma judge answers, up to a minute); a Telegram or Facebook link keeps the link analysis. The result view is `frontend/src/components/NarrativeCheck.tsx`; `GET /api/ext/status` feeds the System page. In demo mode, texts that mention e.g. "puppet" or "Kyiv regime" show the flagged example, anything else the clean one.
- The contract with the backend is `frontend/src/api/types.ts`. The demo data in `frontend/src/demo/` uses the same types and doubles as test fixtures for the backend.
- Colors come from `frontend/src/styles/tokens.css` (the Baltic palette, see `docs/design/palette.html`).
- The build plan and test plan are in `docs/implementation-plan.md`.
