# shared/: what keeps the frontend and backend in agreement

| Folder | What | Who reads it |
|---|---|---|
| `fixtures/` | One JSON file per API answer: exactly what the frontend's demo mode returns, error answers (`{"detail": …}`) included. `manifest.json` describes each file (see below). | Backend tests (B1: models; B3: same answers from the real API), frontend freshness test |
| `cases/` | Rule tables both sides implement: `filters.json` (date ranges, "active between", platform, status, claim search) and `links.json` (accepted links, and `keys`: the one spelling a link is looked up by). | Frontend Vitest and backend pytest |
| `demo/demo-data.json` | All of the frontend's demo data, in the API's shapes. Generated with the fixtures. | Backend demo loader (`backend/demo_data.py`) and the B3 tests |

**What `manifest.json` records for each file:**
- the API call behind it: method, path, query and body;
- the HTTP `status` the backend must answer with;
- `replay`, which says how the answer is compared:
  - `exact`: the real backend must return equal JSON;
  - `shape`: live or AI answers, where only the model has to fit.

**Notes:**
- "Equal JSON" means equal once parsed: numbers compare by value (38 and 38.0 are the same to the frontend), and key order doesn't matter.
- Files keep LF line endings on every operating system (`.gitattributes`), and the checks also tolerate CRLF.
- Invisible characters in the cases (byte-order mark, control characters) are always written as `\uXXXX` escapes, never pasted raw.

**Fixtures are generated, never edited by hand.** After changing the demo data or the API types:

```
cd frontend
npm run export:fixtures           # rewrite shared/fixtures
npm run export:fixtures -- --check   # just check; fails if anything is out of date
```

The export freezes the clock at 14 Nov 2026 (the demo data's dates are relative to "today"), so the files are identical every day.

**Changing a rule:** edit `cases/*.json`. Both test suites pick it up, so the frontend and backend can't disagree.

**Changing the contract:** change `frontend/src/api/types.ts` **and** `backend/veritas/models.py`, then re-export the fixtures. The backend tests fail until both sides match.
