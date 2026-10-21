# Codex Activity Dashboard

A local, read-only dashboard for Codex session metadata. It uses Python 3.9+, a browser, and the standard library; no package installation or external dependencies are required.

## Open in IntelliJ IDEA

Open the `codex-activity-dashboard` folder as a standalone IDEA project. To run it from the IDE, enable the Python plugin and select a Python 3.9+ interpreter. Use `server.py` as the run configuration; command-line arguments are optional. If the Python plugin is unavailable, use the built-in terminal and the command below. IDEA workspace files (`.idea`, `*.iml`) are excluded from the repository.

## Run

```sh
cd ~/Documents/ChatGPT/codex-activity-dashboard
python3 server.py
```

Open the URL printed by the server (default: `http://127.0.0.1:8765/`). Press `Ctrl-C` to stop it. Pass `--port` to select another port; `--port 0` asks the operating system to choose one. Run `python3 server.py --demo` for synthetic data without reading session history.

The default Codex directory is `~/.codex`; use `--codex-home PATH` to select another root. The server binds only to loopback. The API has no write operations, does not read `auth.json`, does not open files from HTTP-provided paths, and never returns message text, prompts, commands, or tool outputs. SQLite is opened with `mode=ro`; if the database is unavailable or incompatible, the dashboard scans `sessions/**/*.jsonl` instead. Every log path is checked against the selected root.

## Panels and interpretation

The dashboard includes session search, model and reasoning effort, token usage, turn timing, context, known rate limits, recognized tools and FileChange paths, confirmed agent links, metadata events, and observed activity. The selected log refreshes every three seconds. Each HTTP request reads at most 8 MiB. While a large file is still loading, the summary is marked provisional and displays progress. Exports repeat bounded reads and mark any data that remains incomplete. An unfinished final JSONL line is held until it is complete; a truncated or replaced file is read again from the start. At most 32 session readers are retained in memory. Event histories and detail lists are bounded while token usage and key aggregates are reduced incrementally.

Each JSONL line is limited to 8 MiB. Longer lines are skipped through the next newline, and the UI and exports indicate possible data loss. Usage deduplication by response ID covers the active event window and the last 3,000 reduced response IDs. Identical counters without an ID are not treated as proof of duplication.

- `NOW` reflects the latest observed turn or tool state. Freshness is based on the latest log event, including event kinds not shown in the UI. After 90 seconds without a new event, the state is marked stale and does not imply that a process is still active.
- Tool elapsed durations can overlap when calls run in parallel. Tool statistics and elapsed time cover only the latest 500 observed calls. Wall time uses available turn boundaries, not summed agent intervals. Recorded turn duration and time to first token are shown separately and only when present in the log.
- `thread_token_usage` is preferred as the cumulative session source; response usage is added only after the latest thread snapshot. `token_count` is the fallback cumulative snapshot. When snapshots disagree, the selected source and difference are shown. Per-model shares use per-response usage and may be unavailable even when the session total is known.
- Cost, cache-write usage, missing rate limits, and unverified file changes remain unavailable. FileChange entries expose paths only, never file contents. Observed skills are labeled as such; the event stream may not contain every skill.
- Token charts use disjoint input-without-cache, cache-hit, and output slices. Reasoning is shown separately as part of output, and cached input is part of input. Missing values appear as `N/A`.
- Available fields and event formats depend on the installed Codex version. SQLite is an internal index without a stable-schema guarantee; scan fallback is used when indexing fails.

Use the `×` button to hide a panel; visibility is stored in `localStorage`. Click **Show all panels** or press `V` to restore all panels. Press `/` to focus session search, `R` to refresh the list, and `Esc` to close the export dialog. Export JSON, Markdown, or HTML for the selected session or all discovered sessions. The print button opens the browser's print dialog, where a PDF can be saved. Exports contain session metadata and omit the event log text.

## Scope and sources

This is an independent clean-room implementation based on behavior descriptions; it does not include source code or a license from the original project. The interface idea is attributed to [osinelnikov/activity-dashboard](https://github.com/osinelnikov/activity-dashboard). Log formats and the local protocol follow the installed Codex CLI; versions and private schemas may change.

| Capability | Status | Boundary |
|---|---|---|
| Session selection, search, and JSONL refresh | Implemented | Session list cache: 10 seconds; detail reads are incremental; SQLite discovery is limited to 2,000 rows |
| NOW state and active tools | Partial | Latest logged state only; no guarantee of live runtime state |
| Tokens and model attribution | Partial | Per-response usage plus cumulative snapshots; unknown fields remain unavailable |
| Turns, tool elapsed time, and wall time | Partial | Tool statistics and elapsed time cover the latest 500 calls; recorded timestamps and durations only; agent intervals are not added to wall time |
| Context, rate limits, and resets | Partial | Only values present in JSONL are shown |
| Compaction | Implemented | Counts top-level `compacted` events, including those outside the retained event window |
| Agent tree | Partial | Uses only parent/child links present in saved events |
| Skills | Unavailable by default | The format does not provide a reliable skill event for every session; arbitrary text is never scanned |
| Cost | Unavailable | The local source does not provide a verified billing ledger |
| Export and print-to-PDF | Implemented | PDF saving uses the browser's standard print dialog |

The demo page was checked through a live loopback HTTP server in a browser. Node.js behavioral checks cover asynchronous UI contracts; saving a printed PDF has not been verified.

## Checks

```sh
python3 -m unittest discover -s tests -v
node --check static/app.js
node tests/test_frontend.js
```

Live-mode smoke checks read the selected local log. Verify the session name against metadata you already recognize. Demo mode is intended for safe UI checks without access to session history.
