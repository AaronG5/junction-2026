# Decode the Noise: browser extension

This Chrome extension (Manifest V3) badges X/Twitter and Facebook posts that our backend flags as **potentially propaganda**, or as showing **coordinated posting**. The two signals are always separate chips. It never says "propaganda", "fake" or "bot", and it never claims who is behind an account.

Contract with the backend: [API.md](API.md).

## Install (load unpacked)

1. Open `chrome://extensions`, then turn on **Developer mode** (top right).
2. Click **Load unpacked** and pick this `extension/` folder.
   - There is no build step: the folder is the extension.
3. Pin "Decode the Noise" to the toolbar and open x.com or facebook.com.

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

### Facebook

Facebook goes through the same pipeline: same worker, same backend API, same badge and popover.
Only finding the posts is different, in `src/fb/`:

- **Posts come from Facebook's data, not its markup.** Facebook's markup hides the post's link
  and time (they appear only on hover), so the extension reads the data Facebook downloads to
  build the feed instead:
  - `src/fb/page-hook.js` runs in Facebook's own page context (`"world": "MAIN"`, at
    `document_start`) and passes a copy of each GraphQL answer that contains posts to the
    content script. It never changes, blocks or sends any request.
  - `src/fb/content.js` also reads the JSON built into the page (the first posts).
  - `src/fb/posts.js` turns each post (`__typename: "Story"`) into an API item: link from
    `permalink_url`, exact time from `creation_time`, author from `actors[0]`, text from
    `comet_sections.content.story.message.text`, group from the `Group` it was posted `to`.
    Ads (`sponsored_data`) and posts without text are left out. Ids are `fb<post_id>`.
- **Matching data to the post on screen.** Facebook doesn't mark which post on screen has which
  id, so a post's text block (`data-ad-rendering-role="story_message"`) is matched to its data by
  the letters and digits of its text (emoji and "See more" cuts differ, the letters don't).
- **The badge goes in its own row right under the post text.**
- **No badges until React has hydrated the page** (`load` + 1.5 s). Adding anything to
  server-rendered posts earlier makes React throw error #418.
- Works on the home feed, groups and pages. Facebook loads a few posts ahead of the screen; like
  on X, a post is sent when it comes within 300 px of the viewport.

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

What is sent: the text of posts you scroll near on X and Facebook, with the post's id, link, author handle, display name, time and language (on Facebook also the group's name). It goes to the backend URL in the popup.

What is not sent: no cookies, no X or Facebook account data, no browsing history, and nothing from other sites. In mock mode, nothing leaves the browser. Results are cached for the browser session only.

## Known limits

- **Selectors may break.** X changes its DOM without notice. Every selector is in `src/selectors.js`.
- **Quoted posts often lack a permalink** in X's markup. Then the quoted item gets a synthetic id (`q<quoting post id>`), and the same quoted post seen inside two different quotes is checked twice.
- **No server-side timing data on X.** On Bluesky, the clock-offset signal (`indexedAt − createdAt`) was our strongest coordination evidence. On X, the only timestamp we have is the one X displays, so coordination on X depends on what the backend already knows about the text and accounts.
- **Promoted posts** are detected by `placementTracking` or an "Ad" label. Localized labels other than en, fi and de may slip through, but ads usually have no permalink, so they are skipped anyway.
- **Mock content is illustrative.** The Telegram links in mock answers point to the real channel but not to real post numbers. The quoted Telegram texts are paraphrases, written for the demo.
- **Facebook's data format may change.** Every field the extension reads is in `src/fb/posts.js`.
- **Facebook shares** are checked as the sharing post; the shared original isn't sent as its own item yet (X quotes are).
- **Facebook posts with very short text** (under 12 letters and digits) can't be matched to the screen reliably and get no badge.
- Desktop web only. The X and Facebook mobile apps cannot run extensions.
