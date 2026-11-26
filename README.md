# Company Leadership RAG Agent

Work in progress. The full README (quickstart, architecture, validation, limitations) lands in the final phase.

Web UI: `uv run leadership ui` (or the VS Code "Launch UI" config), then open http://localhost:8501 (no browser opens by itself, and there is no first-run prompt). The UI is served on localhost only. Chat needs `ANTHROPIC_API_KEY` in `.env`; the Leadership tab works without it.

## Known limitations

- **The eval does not check every claim.** `leadership eval` checks answer patterns, the tool calls made, and that the answer cites a source the tools returned. A wrong detail can still pass. For example, one passing rh-marketing answer said Deepak Rao's profile page is dated by its retrieval date, but only roster pages are. The `expected` text for each case is there for the hand check.
