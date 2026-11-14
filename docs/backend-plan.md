# Backend plan (with tests)

The analysis steps themselves are described in [analysis-cycle.md](analysis-cycle.md). This plan is how we build and test them.

Goal: a backend that serves `/api/*` exactly as the frontend expects ([frontend/src/api/types.ts](../frontend/src/api/types.ts)), reading MySQL and Qdrant and using the local AI through llama-swap. Same way of working as the frontend: **one phase at a time, every phase ends with all tests green, stop for a commit.**

**Ground rules**
- **Python runs through uv only:** `uv run pytest`, `uv run uvicorn …`. Dependencies go in the root `pyproject.toml`.
- **Licences:** permissive only, matching the project's MIT licence:
  - FastAPI (MIT), Uvicorn (BSD), PyMySQL (MIT), qdrant-client (Apache-2.0), httpx (BSD), Pydantic (MIT), pytest (MIT)
  - umap-learn and hdbscan (BSD), only in the clustering phase
- **The frontend contract is the source of truth.** The backend answers in exactly those shapes, and the tests prove it.
- **Everything listens on 127.0.0.1 only.** Errors are always JSON (`{"detail": "…"}`) in plain words, so the frontend can show them as written.
- **Tests never touch real data.** They use a throwaway database (`veritas_test_<random>`) and Qdrant collections prefixed `test_<random>_`, both deleted afterwards.

## How frontend and backend are kept in agreement

This is the most important part of the plan. Three mechanisms:

1. **Shared fixtures.** A frontend script (`npm run export:fixtures`) writes the demo data's API answers to `shared/fixtures/*.json`. Examples: `narratives.json`, `claims.json`, `channel-kanalas_b.json`, `post-report.json`, `stories.json`.
   - The backend's Pydantic models must accept every fixture.
   - The backend, loaded with the same demo data, must return the same JSON.
2. **Shared test cases.** The rules both sides implement live in `shared/cases/*.json`. Both Vitest and pytest read them, so the two sides can't drift apart.
   - `filters.json`: "active between", platform, status, half-open ranges, reversed ranges.
   - `links.json`: which links are accepted.
3. **A freshness check.** A test fails if the committed fixtures differ from what the frontend demo data produces today. If someone changes the contract on one side only, the tests catch it.

**Test types and how to run them**

| Marker | Needs | Run with | When |
|---|---|---|---|
| *(none)*, unit | nothing | `uv run pytest` | always, takes seconds |
| `integration` | Docker MySQL + Qdrant running | `uv run pytest -m integration` | skipped automatically if Docker isn't up |
| `llm` | llama-swap running | `uv run pytest -m llm` | skipped automatically if the AI server isn't up |
| `scrape` | API keys, internet | `uv run pytest -m scrape` | only when run by hand: uses paid credits |

## Code layout

```
backend/veritas/
  main.py            FastAPI app, error handling, serves the built frontend
  config.py          settings from .env (ports, DB credentials)
  models.py          Pydantic models mirroring types.ts
  filters.py         filter rules (same cases as the frontend)
  links.py           Telegram/Facebook link parsing
  db/                connection, migrations runner, queries per screen
  vectors/           Qdrant: collections, search, cluster lookup
  ai/                llama-swap client, prompts (versioned files), analyzers
  pipeline/          analyze-a-link steps
  routes/            one file per API area
backend/tests/       unit + integration + llm tests
shared/fixtures/     contract fixtures exported from the frontend demo
shared/cases/        test cases both sides run
docker/mysql/migrations/0002_*.sql …   schema changes after init.sql
```

## Phases

### B0: skeleton
FastAPI app at `/api`. It also serves the built frontend from `frontend/dist`: any address that isn't `/api` gets `index.html`, so links like `/dashboard/claims` open the app. Settings come from `.env`, and `/api/health` reports whether it's alive.

**Tests**
- `/api/health` answers `{"status": "ok"}`.
- An unknown `/api/...` address returns a **JSON** 404. The frontend shows that as a normal message rather than "server unreachable".
- `/dashboard/claims` and `/story/x` return the app. Asset files (`/assets/*.js`) come back with the right content type.
- When `frontend/dist` is missing, the root page explains how to build it instead of crashing.
- Settings load from `.env` with sensible defaults. Database passwords never appear in any response or log line.

### B1: the contract
Pydantic models for every type in `types.ts`, the fixture export script, and the shared test cases.

**Tests**
- Every file in `shared/fixtures/` validates against its model, and re-serialising it gives identical JSON.
- Field names stay camelCase (`firstSeen`, `changePct`), exactly as the frontend expects.
- The shared filter cases pass in Python (the same table the frontend uses).
- The shared link cases pass in Python (accepted and rejected links).
- The fixtures are fresh: regenerating them from the frontend demo data changes nothing.
- On the frontend: Vitest also reads `shared/cases/*.json`. The current filter and link tests move onto the shared tables.

### B2: database layer
Connection handling (PyMySQL), plus a **migrations runner**. `init.sql` counts as `0001`, and new files in `docker/mysql/migrations/` run once each, recorded in `schema_migrations`. This lets existing installs get schema changes without losing data.

**Migration `0002`** adds what the screens need and the schema lacks:
- `post_clusters.map_x`, `map_y` as `DECIMAL(5,2)`: the post's position on the cluster map, 0–100 with 2 decimals, exactly as in the fixtures. Map points are returned in a fixed order: posts that fit no cluster first, then each cluster's points, clusters in list order.
- `clusters.main_claim_id`: the claim that sets a narrative's status.
- `settings`: key/value storage for the data-source choice and sharing (it moves off the browser in B7).
- `clusters.change_pct` and similar summary fields, so the list doesn't recompute them on every request.

**Migration `0003`** keeps those values sensible: map positions must be 0–100, `first_seen` can't be after `last_seen`, and `change_pct` is wide enough for huge growth.

**When a migration fails:** MySQL commits schema changes immediately, so nothing can be rolled back. The run stops at the failing statement, names the file and the statement, doesn't record the migration and doesn't run later ones. It remembers the attempt (`schema_migration_attempts`). After the fix, the repeated attempt skips statements that already took effect ("column already exists" for `ALTER TABLE … ADD`, and so on) and reports each skip. A first attempt never skips anything, so typos can't hide. Hence: one change per statement.

**Tests** (`integration`)
- On an empty database all migrations apply. Running them again changes nothing.
- A database that already has `init.sql` (like yours) gets only the new migrations, and existing rows survive.
- A failing migration behaves as described above; a fixed one finishes on the next run.
- Mistakes on a first attempt (typo in a DROP, duplicate column, taken name) stop the run.
- Two runs at once on the same database are refused (a named MySQL lock).
- The expected tables, columns, indexes and checks exist after migrating.
- Connections are utf8mb4 and UTC; errors are plain sentences and never show the password.

**Run it:** `uv run python backend/migrate.py` (add `--status` to only list applied and pending migrations). Integration tests use a throwaway `veritas_test_<random>` database made with the root login from `.env` and always dropped; your `veritas_lens` database is never touched.

### B3: read-only API, matching the demo
Endpoints for every screen:
- `/api/narratives`, `/api/claims`, `/api/sources`, `/api/sources/{id}`
- `/api/trending`, `/api/stories/{id}`, `/api/trace?url=…`

**How it works (built):**
- The screens show summaries ("1,284 posts from 212 accounts", a source's share of near copies, a story's origin chain). The analysis jobs work these out and store them (migration `0004_read_models`: summary columns on `claims`, `clusters.stats`, `source_profiles`, `stories`). The API only filters, sorts and counts them, in Python with `veritas/filters.py`, so it follows the shared rule tables exactly (code: `veritas/queries.py`, `veritas/routes/screens.py`).
- Verdicts: the database's finer verdicts show as the four the screens know (`mostly_false` → false, `mixed` → misleading, `unverifiable` → unverified).
- Orders: narratives largest first, claims most recently seen first, sources most near copies first, stories most posts first; ties by id. The frontend's demo sorts the same way.
- `/api/trace?url=…` finds the story whose post is that link, however the link is written (`linkKey()` / `link_key()`, shared cases in `links.json` "keys"). An unknown post → 404 "We haven't collected that post yet…". (The frontend's demo still shows an example story for unknown links.)
- `/api/analyze` and `/api/system` come in B6 and B7; the parity test lists them so they can't be forgotten.

A **demo loader** (`veritas/db/demo.py`) turns `shared/demo/demo-data.json` (exported with the fixtures) into database rows; Qdrant points follow in B4. Run it with `uv run python backend/demo_data.py` (`--remove` to delete it); the launcher's `--demo-data` flag will use it. It moves the demo's dates so its "today" is today. It only loads into an empty database (or one holding demo data, which it replaces). Without being asked, no demo data is ever loaded.

**Tests**
- **Parity (the key test):** after loading the demo data, every manifest entry marked `"replay": "exact"` is replayed with the same method, path, query and body.
  - The backend must answer with the entry's `status`, and with JSON equal to the fixture once parsed (numbers compared by value, key order ignored). This includes the error answers and their plain-language messages.
  - Entries marked `"shape"` (live system status, AI output) only have to validate against their model.
  - If this passes, the screens work against the real backend unchanged.
- **Claim search** follows `matchesQuery` from the shared cases: case-insensitive, with accents significant. MySQL's default collation ignores accents, so either filter in Python or search with a binary collation.
- The shared filter cases are run through every list endpoint, including status counts that ignore the status filter itself.
- An empty database gives empty lists and zero counts, not errors.
- Unknown source or story → JSON 404 with the same message the frontend shows.
- Bad parameters (`status=maybe`, `from=yesterday`) → 422 with a readable message.
- Search uses SQL parameters, never string-building. A search for `'; DROP TABLE` returns nothing and breaks nothing.
- Lists are ordered (largest cluster first, newest claim first) and capped in size.

### B4: vector search (Qdrant)
Create the collections from [docker/qdrant/collections.json](../docker/qdrant/collections.json), with aliases and payload indexes. Then: store post vectors, find similar posts (with platform, language and date filters), and find the nearest cluster centre for a new post.

**How it works (built):** `veritas/vectors.py` talks to Qdrant's REST API with httpx (no extra client library). `uv run python backend/setup_vectors.py` creates what's missing (collections, payload indexes, aliases; `--remove-orphans` also sweeps vectors of deleted posts). A collection made for another embedding model is refused with a plain message instead of being mixed. `veritas/content.py` keeps MySQL and Qdrant in step: a post's vector carries its filter fields (platform, language, published_at, cluster, ...); deleting posts commits the MySQL delete only after Qdrant confirmed, so a failure leaves both untouched. The demo data has no vectors (it has no AI output to embed); B6 fills Qdrant from real posts.

**Tests** (`integration`; hand-made vectors, no AI needed)
- Collection setup can be repeated safely, and the aliases point to the right collections.
- Similar-post search returns the nearest vectors first and respects the filters.
- A new post is assigned to the nearest cluster centre. A post far from every centre is marked "fits no cluster".
- Deleting a post in MySQL also removes its point in Qdrant, so no orphans are left.

### B5: the local AI
A client for llama-swap's OpenAI-style API. It uses the `embedding` model with EmbeddingGemma's prompt templates (stored posts and search queries use different prompts) and the `chat` model with JSON-schema answers. Prompts are versioned files, for example `prompts/claims.v1.md`, and every result records the prompt version and model name.

The analyzers:
- extract claims
- read tone, intent and manipulation techniques (with the exact flagged wording)
- confirm or reject a category match
- translate for display

**How it works (built):** `veritas/ai.py` (client, prompt files) and `veritas/analyzers.py` (the four analyzers); prompts in `backend/prompts/*.vN.md` with `## system` and `## user` sections. Two findings from the live model:
- Gemma 4 "thinks" before answering by default, which used up the token budget and left the JSON empty. Requests switch it off (`chat_template_kwargs: {enable_thinking: false}`); answers then take under a second.
- llama.cpp builds a grammar from the JSON schema, and a long `maxLength` makes one too big to build. The schema sent to the model leaves lengths out; they're checked when the answer is read. Answer schemas are strict ("yes" isn't true, "7" isn't 7).
- Results record the real model behind the alias (e.g. `gemma-4-E2B-it`, not `chat`) and the prompt version (`claims.v1`).

**Tests**
- **Unit tests against a fake AI server:**
  - prompts fill in correctly
  - valid JSON answers parse
  - invalid JSON triggers one retry and then a clear error
  - a technique's "flagged wording" must actually appear in the post, otherwise it's dropped
  - timeouts give a readable error
- **Live tests** (`llm`) against your running llama-swap:
  - an embedding has 768 numbers
  - a Lithuanian sentence and its English translation are more similar than two unrelated sentences
  - one analyzer returns valid JSON on a real post

### B6: Analyze a link
`/api/analyze` runs the steps of the [analysis cycle](analysis-cycle.md):
1. read the link
2. find the post
3. embed it
4. find similar posts and how they relate (translation, copy, forward)
5. check claims
6. Gemma's reading

Results are stored, so the same link is answered instantly the next time.

**Progress: a job, plus polling.** `POST /api/analyze` starts a job and returns its ID at once. The screen asks `GET /api/analyze/{id}` every second for the current step, then gets the report. This is simple and robust; speed isn't the priority. It needs a small change in the frontend's API client.

**Collecting posts.** There's one "collect a post" interface with two providers behind it:
- **Oxylabs** for Facebook and Telegram.
- **context.dev** for news sites and evidence pages.

API keys go in `.env` (`OXYLABS_USERNAME`, `OXYLABS_PASSWORD`, `CONTEXT_DEV_API_KEY`). Without keys, the backend can still analyze posts already in the database, and says so in plain words otherwise. The team's parser that writes scraped data to MySQL plugs in here too.

**How it works (built):**
- `veritas/analysis.py` runs the steps and builds the report; `routes/analyze.py` has `POST /api/analyze` and `GET /api/analyze/{id}` (an `AnalysisJob`: status, step, report or a plain error). The frontend's client polls every second; its demo mode is unchanged. Migration `0005` turns `analysis_requests` into the job record.
- Answered at once, before any job: not a link (400), a channel/page link or a share link that doesn't name a post (400), post not collected and no scraper keys (404), AI not running (503), database down (503). Failures during a job end it with a plain message and a status (AI stopped 503, unusable AI answer 502, scraper errors their own, no text 422).
- Stored: the post's vector (Qdrant), similar posts as `post_relations`, claims (matched to known claims by vector, so each is checked once) with `post_claims`, `post_analyses` (claims, reading, translation; prompt version + model), `post_categories`, and the report itself. The same post, however its link is written, is answered from the stored report without asking the AI again. A failed analysis isn't reused.
- New claims start as "not checked yet": checking them against sources (web search) isn't part of this plan yet.
- Post ids come from the link (`veritas/links.py parse_link`): Telegram `channel/123` (private channels `-100<id>/123`), Facebook `page_123`, `owner_story`, `group_123`. The Telegram widget also carries the channel's internal id (`telegram_channel_id()`), for when ids are unified.
- Collecting: `veritas/collectors.py`. The Telegram parser was built on real saved widget pages (`backend/tests/samples/`). The Facebook and article samples are constructed (no keys yet), and context.dev's endpoint shape comes from public listings; both should be checked against real responses once keys are in `.env`.
- Frontend changes: the client polls the job; `PostReport.cluster` can be null ("Not part of a known narrative yet"); the Analyze page asks for a post link.

**Tests**
- **Scraper adapters, against saved real responses** (no live calls, no API costs): a saved Oxylabs Facebook response becomes the right post, account, time and media. A context.dev article becomes the right text and author. Empty, private, rate-limited and malformed responses each give their own error.
- **Live scraper tests** (`scrape` marker, run by hand, cost credits): one real Facebook post and one real news article.
- **Link parsing:** `t.me/kanalas_b/4521` → Telegram, channel `kanalas_b`, message 4521. This covers Facebook page, group and post links, and the same post written different ways (with or without `https`, `www`, tracking parameters).
- **The full pipeline with a fake AI and a test database:** every step runs, results are stored, and the report matches the contract.
- Analyzing the same link twice reuses the stored result, with no second AI call.
- **Each failure gives its own message:**
  - not a supported link → 400
  - post not collected yet → 404 with the "not collected" message
  - AI server down → 503 "the local AI isn't running"
  - database down → 503
- A slow analysis can't block other requests.

### B7: System and settings
- `/api/system` shows which model is loaded (read from the launcher's state), the hardware, and live health checks of llama-swap, MySQL and Qdrant.
- `/api/settings` stores the data-source choice and sharing in the database, replacing the browser-only setting. This needs a small frontend change.

**How it works (built):** `veritas/system.py` checks llama-swap, MySQL and Qdrant at the same time with 2-second timeouts; the model, hardware and upgrade come from the launcher's state file (`.local_ai/state.json`, written by the launcher since B8), so the backend never imports the launcher. `veritas/app_settings.py` keeps the data mode and sharing in the `settings` table. The frontend loads them at start and saves each change; a refused change is undone with the backend's message, and without a backend the choice stays in the browser (and says so). Demo mode keeps them in the browser, with the same rule.

**Tests**
- Each service down shows as stopped. The page still loads.
- Settings survive a backend restart.
- Turning sharing on in local-only mode is refused by the backend too (not just by the screen).
- No password or secret ever appears in `/api/system`.

### B8: the launcher runs everything
New launcher steps:
- apply database migrations
- create the Qdrant collections
- build the frontend when Node is available and the build is out of date (otherwise use a prebuilt copy)
- start the backend in the background on `127.0.0.1:8000` (`BACKEND_PORT` in `.env`)
- `--demo-data` loads the demo data
- `--stop` also stops the backend

**How it works (built):** `local_ai/backend.py` runs the backend's own tools as separate processes with the same Python (`backend/migrate.py`, `setup_vectors.py`, `demo_data.py`, `run.py`), so the launcher never imports the backend. Migrations and vector setup wait up to 90/60 s for MySQL/Qdrant to come up. The dashboard is rebuilt only when a file in `frontend/src`, `package.json` etc. is newer than `dist/index.html`. The backend runs hidden in the background (log: `logs/backend.log`, pid: `.local_ai/backend.pid`) and is restarted only when `.env` or the backend's code changed. The launcher also writes the hardware summary and an upgrade suggestion into its state file for the System page. New flags: `--demo-data`, `--no-backend`; `--stop` and `--status` include the dashboard.

End-to-end run on the dev machine (`python -m local_ai --demo-data -y`): all 13 steps, every screen's API answered from the real server, the System page showed the real GPU and an upgrade suggestion. It found one bug the fakes missed (the health check compared bytes with text), now covered by a test.

**Tests**
- **Unit:** the new steps with fakes: migrations called once, a build skipped when up to date, backend restarted only when its config changed, demo data loaded only with the flag.
- **End to end on this machine:** `launch.bat --demo-data`, then every screen at `http://127.0.0.1:8000` shows data (screenshots like the frontend phases), then `launch.bat --stop` leaves no backend process behind.

### B9: clustering (after B8, decided)
A batch job:
1. Group posts with UMAP + HDBSCAN.
2. Store each post's map position, its cluster, and its rank (typical vs edge).
3. Store each cluster's centre in Qdrant.
4. Name each cluster with Gemma from its 5 most typical posts.
5. Link clusters to narratives across runs.

This needs heavier packages, so it'll be an optional install: `uv sync --extra clustering`.

**How it works (built):** `veritas/clustering.py`, run with `uv run python backend/cluster.py --days 7` after `uv sync --extra clustering` (umap-learn + scikit-learn's HDBSCAN; scikit-learn's version avoids the separate `hdbscan` package, which can need a compiler on Windows).
- UMAP (5 dimensions for grouping, 2 for the map; fixed seed, `compatibility_layout=True` so results don't change with UMAP's next default) and HDBSCAN (`min_cluster_size = max(5, posts/40)`).
- UMAP pulls lone posts into the nearest group, so membership is checked again in the original 768 numbers: a post less similar than 0.5 to its group's centre fits no group. (Found with the synthetic test: without it, all 10 scattered posts landed in groups.)
- Each group: centre (stored in Qdrant `clusters`, also used to assign new posts at once), ranks (1 = most typical), 2 typical and 1 edge post, platforms and language shares, accounts, first/last seen, main claim (the claim most of its posts make), and a name and summary from Gemma (`prompts/cluster_name.v1.md`).
- Narratives across runs: a new group whose centre is at least 0.85 similar to one from the previous run continues that narrative, and gets its week-on-week change; others start a new narrative.
- Fewer than 20 posts in the window: no run, "no clusters" message. A failure marks the run failed, and the screens keep showing the last good run.
- Not yet run on real data: the demo data has no vectors. Posts analyzed through "Analyze a link" (B6) or collected by the team's scraper get vectors, and then appear in the next run.

**Tests**
- **Synthetic data with known groups:** three clear groups are found, scattered points are marked as noise, and the same input always gives the same result.
- Typical posts are the ones closest to the centre, and edge posts the farthest.
- Re-running on a newer week links the same story to the same narrative.
- A week with too few posts gives "no clusters" instead of nonsense.

## Not in this plan
- Scrapers for Telegram, Facebook and news sites.
- The central server: public hosting, user accounts, syncing with self-hosted installs.
- The browser extension.

The backend is shaped so each can be added without rewrites: the scraper plugs into B6's "fetch post", and central/hybrid mode plugs into the API client.

## Decisions

| Question | Decision |
|---|---|
| Progress while analyzing | Job + polling. Speed is not the priority; optimize later. |
| Scrapers | Oxylabs and context.dev, behind one "collect a post" interface (B6). The team writes the parser and MySQL writer. |
| Database name | Renamed to `veritas_lens` (database and login user). Done on the dev machine; `.env`, `.env.example`, the launcher defaults and docker-compose updated. |
| Clustering | After B8. |
| Automatic test runs (GitHub Actions) | Not needed. Tests are run locally at the end of every phase. |

## Sample data: `articles.json`

2,997 articles scraped from **respublika.lt** (a news site, not Facebook):
- Only **33 have the full text.** The rest have a headline and teaser.
- 307 have no date, and dates go back to 2016.

Planned use:
- **Real input for tests:** the 33 full articles in Lithuanian are ideal for B5 (fingerprints, Gemma analyzers) and for testing the context.dev adapter's output format. This is better than invented text.
- **Temporary data:** posts and a source (`respublika.lt`, platform Website) for the screens. No verdicts are invented: real articles stay "not checked" until the real cycle has checked them.
- It stays out of the public repository (see the note in chat), for example in `shared/samples/`, which is ignored by git.
