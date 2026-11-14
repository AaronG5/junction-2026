# Decode the Noise: browser extension

This Chrome extension (Manifest V3) badges X/Twitter posts that our backend flags as **potentially propaganda**, or as showing **coordinated posting**. The two signals are always separate chips. It never says "propaganda", "fake" or "bot", and it never claims who is behind an account.

Contract with the backend: [API.md](API.md).

## Install (load unpacked)

1. Open `chrome://extensions`, then turn on **Developer mode** (top right).
2. Click **Load unpacked** and pick this `extension/` folder.
   - There is no build step: the folder is the extension.
3. Pin "Decode the Noise" to the toolbar and open x.com.

It works in Chrome, Edge, Brave and other Chromium browsers, version 116 or later.

## Demo without X or a backend

- **Offline demo feed:** `demo/feed.html` copies X's DOM shape and `data-testid`s. It has 10 posts, including:
  - the Nvidia "CIA operation" story (source match, then the judge's flag, plus coordination);
  - the "Kyiv regime is an American puppet" narrative (EUvsDisinfo, with an RT echo);
  - a Finnish machine-translated persona-farm post;
  - a quote, a repost, an ad, a short post, and a post whose check fails.
- **Opening it:** either of these works.
  - Serve it: `python3 -m http.server -d demo 8765`, then open `http://localhost:8765/feed.html`.
  - Open the file directly: `file:///…/extension/demo/feed.html`. First turn on **Allow access to file URLs** in the extension's details.
- **Mock answers:** pages that are not X always use the mock backend.
  - Add `?live` (`feed.html?live`) to send the demo posts to the configured backend instead.
- **Demo buttons:** **Load 200 more** (infinite scroll), **Simulate route change** (SPA navigation) and **Dark / light**.

## Modes and settings (toolbar popup)

| Setting | Default | What it does |
|---|---|---|
| On/off | on | Off removes every badge at once and stops scanning. |
| Show all badges | off | A tiny grey dot on posts where nothing was found. |
| Sensitivity | normal | **Strict** shows the judge's flag only when `score ≥ 0.75`. Source matches and coordination are shown in both modes. |
| Mock mode | off | On X too, answer from `src/mock.js` and send nothing over the network. About 5% of posts get flagged, chosen by a hash of the id. The fast path takes 300 ms, the judge 3 s. |
| Backend URL | `https://augustasrink--ppg-ext-web.us-east.modal.run` | Where `/api/ext/*` lives. |

The settings live in `chrome.storage.sync`. The counters (scanned, flagged) cover the current browser session.

### Pointing it at a backend

Type the base URL in the popup and press **Save**. The popup then pings `GET /api/ext/result?ids=` and reports whether the server answered.

- `*.modal.run`, `localhost` and `127.0.0.1` are allowed out of the box.
- Any other origin triggers a one-time permission prompt.
- The extension calls the backend only from its service worker, so the server needs **no CORS headers** for the extension.
- The backend has to answer within 10 s per request.

## What the badge shows

| State | Look |
|---|---|
| Checking | A small grey spinner next to the timestamp. |
| Nothing found | Hidden, or a grey dot with "show all". |
| Source match | A red chip: "Copies pro-Kremlin Telegram post". It appears from the fast path, before the judge finishes. |
| Potentially propaganda | An orange chip with the judge's verdict. It is red with a ⧉ icon when a Telegram source match also exists. |
| Coordinated | A separate purple chip ("Coordinated"), for the account- or cluster-level signal. |
| Unavailable | A grey outline chip: the backend did not answer, failed, or left the id out. |

Clicking a chip (or pressing Enter on it) opens a popover with:
- the labels;
- the Telegram source post (channel, date, similarity, link, snippet);
- the matched narrative with its source and similarity;
- the state-media echo line ("This narrative aired 253× on RT, Jan–Oct 2024");
- technique tags with quoted phrases;
- the reason;
- coordination reasons;
- the disclaimer footer, with **Report a mistake** and **Looks right**.

The popover traps focus and closes on Esc or an outside click.

When the post is too narrow for full chips, they step down: first short labels, then icons only. The full wording stays in `aria-label` and `title`.

## How it works

```
content script (x.com)                     service worker                         backend
  MutationObserver (1, debounced 120 ms)
  -> parse article  (src/parse.js, src/selectors.js)
  -> IntersectionObserver (1, ±300 px)  -> port "scan" -> cache hit? reply
                                                         else batch (16 / 400 ms) -> POST /api/ext/batch
                                                         pending -> poll 2 s, ≤60 s -> GET /api/ext/result
  badge (Shadow DOM)  <- port "results"  <-------------- push to the tabs that asked
```

- `src/selectors.js`: **every X selector**. When X changes its markup, fix this file.
- `src/parse.js`: article → API item:
  - id and URL from the status link;
  - handle and display name;
  - text, with emoji alt text included;
  - time, language, and whether it is a post, repost or quote;
  - the quoted post as its own item.
- `src/view.js`: result → chips. This is pure logic and holds the wording.
- `src/ui.js`: badge and popover, both in Shadow DOM, with the Baltic tokens from `frontend/src/styles/tokens.css`. The badge follows X's light or dark theme.
- `src/content.js`: discovery, dedupe by id, rendering, and SPA and route changes.
- `src/background.js`: batching, caching (memory and `chrome.storage.session`), polling, error handling and counters.
- `src/mock.js`: the mock backend.

The badge has a fixed 20 px height and does not wrap. It sits in the timestamp's row, so it never changes the height of a post.

## Tests

```
npm install          # jsdom + playwright-core (dev only; the extension itself has no dependencies)
npm test             # all three below
npm run test:unit    # jsdom: parser on a saved X markup fixture, view model, badge/popover, mock
npm run test:e2e     # headless Chromium with the extension loaded, demo feed in mock mode, screenshots in test/out/
npm run test:live    # same, against a stub HTTP backend that follows API.md (also checks the backend-down case)
```

The e2e tests use Playwright's Chromium from `~/.cache/ms-playwright`. To use another browser, set `CHROMIUM=/path/to/chrome`. They load the extension in Chrome's new headless mode (`--headless=new`).

## Privacy

What is sent: the text of posts you scroll near on X, with the post's id, link, author handle, display name, displayed time and language. It goes to the backend URL in the popup.

What is not sent: no cookies, no X account data, no browsing history, and nothing from other sites. In mock mode, nothing leaves the browser. Results are cached for the browser session only.

## Known limits

- **Selectors may break.** X changes its DOM without notice. Every selector is in `src/selectors.js`.
- **Quoted posts often lack a permalink** in X's markup. Then the quoted item gets a synthetic id (`q<quoting post id>`), and the same quoted post seen inside two different quotes is checked twice.
- **No server-side timing data on X.** On Bluesky, the clock-offset signal (`indexedAt − createdAt`) was our strongest coordination evidence. On X, the only timestamp we have is the one X displays, so coordination on X depends on what the backend already knows about the text and accounts.
- **Promoted posts** are detected by `placementTracking` or an "Ad" label. Localized labels other than en, fi and de may slip through, but ads usually have no permalink, so they are skipped anyway.
- **Mock content is illustrative.** The Telegram links in mock answers point to the real channel but not to real post numbers. The quoted Telegram texts are paraphrases, written for the demo.
- Desktop web only. The X mobile apps cannot run extensions.
