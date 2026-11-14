# junction-2026
Junction 2026: European Tech Renaissance project for Synthetic Logic LAB team

## Self-hosted setup

Everything runs on your own computer: the AI model, the search index and the database.

**Start it**

- Windows: double-click `launch.bat`
- Linux / macOS: run `./launch.sh`

That's it. The first start checks your computer, picks the Gemma model that fits it best, downloads it, and starts everything. If the download is interrupted, start it again and it continues where it stopped. Later starts take seconds and work offline.

**You need:** [uv](https://docs.astral.sh/uv/) (the launcher offers to install it; uv brings its own Python) and [Docker Desktop](https://docs.docker.com/get-started/get-docker/) for the databases.

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

The AI is served at `http://127.0.0.1:8080/v1` as an OpenAI-compatible API, with two models: `chat` and `embedding`. Settings live in `.env` (created on first start).

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

- The contract with the backend is `frontend/src/api/types.ts`. The demo data in `frontend/src/demo/` uses the same types and doubles as test fixtures for the backend.
- Colors come from `frontend/src/styles/tokens.css` (the Baltic palette, see `docs/design/palette.html`).
- The build plan and test plan are in `docs/implementation-plan.md`.
