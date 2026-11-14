# junction-2026
Junction 2026: European Tech Renaissance project for Synthetic Logic LAB team

## Self-hosted setup

Everything runs on your own computer: the AI model, the search index and the database.

**Start it**

- Windows: double-click `launch.bat`
- Linux / macOS: run `./launch.sh`

That's it. The first start checks your computer, picks the Gemma model that fits it best, downloads it, and starts everything. If the download is interrupted, start it again and it continues where it stopped. Later starts take seconds and work offline.

**You need:** Python 3.10+ (the Windows launcher offers to install it) and [Docker Desktop](https://docs.docker.com/get-started/get-docker/) for the databases.

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
