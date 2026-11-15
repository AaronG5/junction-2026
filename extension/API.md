# Extension ↔ backend API (v0.1)

The browser extension talks to the backend through three endpoints under `{baseUrl}`.
All requests come from the extension's service worker: no cookies, no credentials, `Content-Type: application/json`.
The client waits up to **10 s** per request.

The mock backend in `src/mock.js` implements this contract exactly. The stub server in `test/e2e-live.mjs` does too, and serves as an executable example.

## POST /api/ext/batch

The client sends up to 16 items per request: a batch is sent when it reaches 16 items, or 400 ms after the first item arrives.

```json
{
  "client": "ext/0.1",
  "items": [
    {
      "id": "1890412233445566771",
      "url": "https://x.com/eu_insider_news/status/1890412233445566771",
      "author": "eu_insider_news",
      "author_name": "EU Insider News",
      "text": "⚡ The Nvidia scandal is a CIA operation. …",
      "lang": "en",
      "time": "2026-11-14T17:48:00.000Z",
      "kind": "post",
      "quoted_id": null
    }
  ]
}
```

Item fields:

| field | notes |
|---|---|
| `id` | The X status id (digits). For a quoted post that X renders without a permalink, the id is synthetic: `q<id of the quoting post>`. Ids match `[A-Za-z0-9]{1,40}`. |
| `url` | Canonical `https://x.com/<handle>/status/<id>`. `null` for a synthetic quoted id. |
| `author`, `author_name` | The handle without `@`, and the display name as rendered. Emoji appear as their alt text. |
| `text` | The post text: text runs, emoji alt text, and link text as displayed. X shortens links, so the text can contain a truncated URL. |
| `lang` | X's own guess (`lang` attribute on the text). Can be `null`, `und`, `qme` (emoji only) and so on. |
| `time` | The ISO timestamp X displays (`time[datetime]`). Not a server-side indexing time. |
| `kind` | `post`, `repost` or `quote`. A repost of a quote is reported as `quote`. |
| `quoted_id` | On a `quote` item, the id of the quoted post. The quoted post is sent as its own item in the same batch, with `kind: "post"`. |

Posts shorter than 40 characters are not sent, and neither are promoted posts. A quote can still be sent if the quoted post is long enough. Each id is sent at most once per page.

### Facebook items

Facebook posts use the same endpoints and item shape, with these differences:

| field | on Facebook |
|---|---|
| `id` | `fb<post_id>`, e.g. `fb3674462936037307`. The `fb` prefix keeps them apart from X ids. |
| `url` | Facebook's own permalink: `https://www.facebook.com/groups/<group>/posts/<id>/`, `/<page>/posts/pfbid…`, `/reel/<id>/`, `/<page>/videos/<id>/` or `permalink.php?story_fbid=…&id=…`. |
| `author` | The profile's handle (`vilma.mamaityte`), or its numeric id for `profile.php?id=` profiles. |
| `text` | The full text, also the part behind "See more". Emoji are the characters themselves. |
| `lang` | Always `null`: Facebook doesn't say. |
| `time` | When the post was published, exact to the second (Facebook's `creation_time`). |
| `kind` | Always `post` for now. Shares are sent as the sharing post. |
| `platform` | `"facebook"` (only on Facebook items; X items have no `platform`). |
| `group` | The group's name for posts in a group, else `null` (only on Facebook items). |

### Response

```json
{ "results": [ {
  "id": "1890412233445566771",
  "status": "pending",
  "content": {
    "label": "none",
    "score": 0.0,
    "narrative": { "text": "The Kyiv regime is an American puppet", "source": "euvsdisinfo", "sim": 0.93 },
    "techniques": [ { "code": "LL", "name": "loaded language", "quote": "a CIA operation" } ],
    "reason": "One sentence.",
    "state_media_echo": [ { "text": "…", "source": "rt", "n_aired": 253, "first": "2024-01-07", "last": "2024-10-02" } ]
  },
  "source_match": { "strong": true, "telegram": [ { "channel": "two_majors", "url": "https://t.me/two_majors/58214", "date": "2026-11-13T18:49:44Z", "sim": 0.97, "text": "…" } ] },
  "coordination": { "label": "coordinated", "reasons": ["same text posted by 6 other accounts in the last hour"], "n_similar_accounts": 6 }
} ] }
```

- `status`
  - `done`: final.
  - `pending`: the judge has not finished yet. The fast-path fields (`source_match`, `content.narrative`, `coordination`) may already be filled.
  - `error`: the backend could not process the item.
- `content.label`
  - `potentially_propaganda` or `none`.
  - Only the judge sets it. While `pending`, it is ignored (send `"none"` or leave it out).
- `content.score`
  - From 0 to 1.
  - Used only by the client's **strict** mode, which shows the judge's flag only when `score ≥ 0.75`.
- `source_match.strong`
  - Shows the source-match chip immediately, in both modes.
  - Telegram candidates with `strong: false` are not shown.
- `coordination`
  - `null` means not computed or no signal.
  - `label: "coordinated"` shows a separate coordination chip.
- `narrative.source` and `state_media_echo[].source`
  - Lower-case keys.
  - The UI prettifies `euvsdisinfo`, `rt`, `sputnik`, `tass` and `ria`, and shows any other key as sent.
- Links (`telegram[].url`) are shown only if they are `https:`.
- All strings are rendered as text, never as HTML.

**Every requested id should appear in `results`.** The client treats a missing id as `error` and shows "Check unavailable". It may ask for that id again 30 s later, if the post is still on screen after a reload.

## GET /api/ext/result?ids=a,b,c

Returns the same `{results: [...]}` shape as the batch response.

The client polls every **2 s** for ids that are still `pending`, with up to 40 ids per request. It stops after **60 s**. A post that is still pending then gets the `timeout` state: chips that already arrived stay, and the popover says the text check did not finish.

While polling, the client ignores a per-item `status: "error"` and keeps polling until the 60 s deadline. Unknown ids may be returned as `error`.

## POST /api/ext/feedback

```json
{ "tweet_id": "1890412233445566771", "verdict_shown": "source_match+potentially_propaganda+coordinated", "user_says": "wrong", "note": "optional, ≤500 chars" }
```

- `verdict_shown` lists what the user saw, as names joined with `+`:
  - `source_match`, `potentially_propaganda`, `coordinated`;
  - a name about a quoted post is prefixed with `quoted:`;
  - otherwise one of `clean`, `unavailable`.
- `user_says` is `wrong` ("Report a mistake") or `right` ("Looks right").
- The response is `{ "ok": true }`.

## POST /api/ext/deep, GET /api/ext/deep/{id} (in-depth analysis)

Started only when the user clicks **Analyze**. It goes to the **in-depth analysis URL** (popup setting
`deepUrl`, default `http://127.0.0.1:8000`: Veritas Lens on the user's computer, because it needs the
local Gemma). Implemented in `backend/veritas/deep.py`; `src/mock.js` (`MockDeep`) follows the same shape.

```json
{
  "item": { "id": "...", "url": "...", "author": "...", "author_name": "...", "text": "...", "lang": "en",
            "time": "...", "kind": "post", "quoted_id": null, "platform": "facebook", "group": "..." },
  "shared": { "author": "...", "author_name": "...", "url": "...", "time": "...", "text": "...", "platform": "x" },
  "context": { "narrative": {...}, "state_media_echo": [...], "source_match": {...} }
}
```

- `shared`: the quoted (X) or shared (Facebook) original, or `null`.
- `context`: what the fast check (`/api/ext/batch`) already returned for the post, so the trace can use it.
  The client waits up to 10 s for that result before starting.
- The answer is the job; the same post asked for again gets the same job. Errors: 400 no text, 503 the local
  AI isn't running, 404 (GET) unknown job.

The client polls `GET /api/ext/deep/{id}` every 1.5 s (for up to 5 minutes) until `status` is `done` or `error`:

```json
{
  "id": "9f2c…", "post_id": "fb3674…", "status": "running", "step": 1, "error": null,
  "steps": ["Finding propaganda patterns", "Checking the claims", "Tracing where it came from"],
  "patterns": { "items": [ { "quote": "a CIA operation", "technique": "Conspiracy framing", "tier": "clear", "why": "…" } ],
                "overall": "…", "model": "gemma-4-E2B-it", "prompt": "patterns.v1" },
  "truth": { "items": [ { "claim": "…", "quote": "…", "verdict": "refuted", "basis": "sources", "explanation": "…",
                          "searched": ["Zelensky selling Ukrainian children", "…"],
                          "sources": [ { "n": 1, "kind": "disinfo", "source": "EUvsDisinfo", "title": "…", "text": "…",
                                         "url": "https://euvsdisinfo.eu/report/…", "date": "2025-07-15", "score": 0.88 } ] } ],
             "basis": "Checked against trusted documents and documented disinformation, never against the post itself.",
             "model": "…", "prompt": "claimsearch.v1+verdict.v1" },
  "trace": { "summary": "The earliest trace found is @two_majors (…)…",
             "nodes": [ { "kind": "telegram", "title": "Telegram post", "who": "@two_majors", "where": "…", "time": "2026-11-13T18:49:44Z",
                          "url": "https://t.me/…", "sim": 0.97, "text": "…", "note": "…" } ] }
}
```

- `step`: 0–2 while running (index into `steps`), 3 when done. Each part appears as soon as it is ready.
- `patterns.items[].tier`: `clear`, `likely` or `possible`, clearest first. Every `quote` is the post's own words
  (Gemma's quotes that aren't in the post are dropped); the client highlights them.
- `truth.items[]`: a fact check with a search tool (`backend/veritas/evidence.py`), never the post itself:
  Gemma lists the claims with search queries (`searched`), the tool searches the trusted documents in `data/evidence/`
  and the narrative bank's documented disinformation, and Gemma judges each claim against those numbered sources only.
  `verdict`: `supported`, `refuted`, `misleading` or `unverified`; `basis`: `sources` (then `sources` lists what it
  cites, `kind` `document` or `disinfo`), `general_knowledge` (basic long-established facts only) or `none`
  (no source found: always `unverified`). A `quote` not in the post is `""`.
- `trace.nodes[].kind`: `narrative` (undated background, first), `state_media`, `telegram`, `similar` (earlier posts
  in the Veritas Lens database), `original` (the shared or quoted post), `this` (this post, with how it reached the
  feed). Oldest first; nodes after `this` appeared later. Only `https:` links are kept.

## POST /api/ext/save

Same body as `/api/ext/batch`; answers `{ "queued": n }`. Only stores the posts, no check. The extension
sends every batch here too, to the in-depth analysis URL (Veritas Lens on the user's computer), when the
popup's **Save posts on this computer** is on and the badges come from another server.

Posts that reach `/api/ext/batch` or `/api/ext/save` are kept in MySQL and embedded into Qdrant
(`backend/veritas/ext_store.py`), so the collection grows while people browse. Off with `EXT_SAVE_POSTS=off`.

## Client behaviour the backend can rely on

- Results are cached by id in the worker's memory and in `chrome.storage.session`. The cache is per backend URL, and mock results never mix with real ones.
- A posted id is not re-posted while it is pending, or once it is done.
- Labels shown to users:
  - "Potentially propaganda";
  - "Copies a known pro-Kremlin Telegram post";
  - "Coordinated posting";
  - "Check unavailable".
- Content and coordination are always separate chips.
