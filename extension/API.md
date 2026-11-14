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

## Client behaviour the backend can rely on

- Results are cached by id in the worker's memory and in `chrome.storage.session`. The cache is per backend URL, and mock results never mix with real ones.
- A posted id is not re-posted while it is pending, or once it is done.
- Labels shown to users:
  - "Potentially propaganda";
  - "Copies a known pro-Kremlin Telegram post";
  - "Coordinated posting";
  - "Check unavailable".
- Content and coordination are always separate chips.
