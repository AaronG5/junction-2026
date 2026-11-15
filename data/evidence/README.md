# Trusted evidence for the fact check

The in-depth analysis (the extension's **Analyze** button) checks each claim against the
documents in this folder, never against the post itself. Put here only material from
sources you trust: fact-checks, official statistics, encyclopedia extracts, articles from
reliable outlets. The search tool is in `backend/veritas/evidence.py`.

Formats (any file name, subfolders allowed):

- `*.jsonl`: one document per line:
  `{"title": "...", "text": "...", "url": "https://...", "source": "LRT", "date": "2026-11-14"}`
- `*.json`: a list of such documents (or `{"documents": [...]}`).
- `*.md` / `*.txt`: one document per file; the first line is the title, the folder name the source.

The search is by keywords (accents and letter case don't matter). A claim needs several of its
words in a document for the document to count. Files are re-read when they change: no restart needed.

Never evidence, even if put here: documents from known propaganda outlets (Telegram, RT, Sputnik,
TASS, RIA, Respublika, Būkime vieningi and others in `UNTRUSTED_DOMAINS`), and the scraped data in
`data/parsed/`. The narrative bank's documented disinformation (EUvsDisinfo and others) is searched
as well, as evidence that a claim is a known false narrative.
