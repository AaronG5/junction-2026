Integrate the Oxylabs Web API into this project for live web search and page reading.

Read https://developers.oxylabs.io/products/web-api/for-agents.md and follow
it. It has the base URL, auth, both endpoint schemas, reference code, the error and retry
rules, and the checklist to satisfy. If the page does not load, stop and tell me; do not
reconstruct the API from memory.

Decide the path and tell me before writing anything:

- Will this project's own code call the API at runtime? -> write the client the page
  specifies, matching how this codebase already makes outbound HTTP calls.
- Is the web access for you during this session rather than for the project's code?
  -> install the MCP server and the skills, the way the page describes.

The key is in OXYLABS_WEB_API_KEY or a .env here. If it is in neither, ask me. Never
print the key: not in code, logs, tests, or your replies to me.

Prove it works before writing tests: make one real search, scrape one URL from the
results, and show me both request ids. If a call returns 401, stop and tell me.

When a scrape comes back empty or skeletal, retry it once with run_js: true. If it is still
empty and the site is on a country TLD (.lt, .es, .co.uk), retry once more with run_js and
location set to that country, then report the page as unreadable.

Then add a test for the retry and error mapping against a mocked transport and run it.
If you cannot run commands in this environment, say so once and give me the exact
commands to run instead.

Report back: the path you took, each checklist item on the page marked done or not,
what I still need to do myself, and anything you could not finish. Do not ask me
whether to proceed; do the work.
