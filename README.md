# NexAgentOps Knowledge

A local, rebuildable keyword search index for existing personal knowledge exports. This first contribution uses only Python's standard library and SQLite FTS5. It does not access Apple Notes, a browser, a network service, or an LLM.

## Architecture review

| Existing source | Reusable boundary | Integration decision |
| --- | --- | --- |
| `apple-notes-pipeline` | Read-only JXA export, normalized `notes.jsonl`, stable source IDs when Notes supplies them, metadata-only diffs and count-drop validation | Consume current normalized JSONL. Do not run its exporter or index generated Markdown separately. Generated fallback IDs may change after edits. |
| `chatgpt-archive` | Canonical conversation JSON, stable canonical conversation ID, content-addressed revisions, rebuildable SQLite FTS5, bounded resumable capture | Consume canonical `raw/*.json`, never its operational database or browser session. Preserve partial-capture limitations from the source. |
| User Markdown | Plain files with filesystem path and modification time | Read only explicitly selected directories. Keep these separate from generated source Markdown. |

The unified SQLite database is a disposable projection. Existing JSONL, canonical conversation JSON, and Markdown files remain authoritative. Document identity is `(source, source_id)`; a later sync updates an existing row rather than creating another. The adapters reject duplicate IDs within one input. Similar text from distinct source identities remains separate so attribution is not lost. This first contribution preserves records missing from later snapshots; a missing export is not proof of deletion.

## First three contributions

1. **Implemented: local keyword search.** Source adapters, a transactional SQLite FTS5 index, incremental hash checks, attribution, timestamps, and a CLI. Synthetic tests cover the three source types, repeat syncs, updates, duplicate IDs, and missing-record preservation.
2. **Next: source manifests and reconciliation.** Track each source's last successful complete snapshot, source-specific missing status, stale results, and explicit removal policy. Test interrupted and partial exports, source disappearance, fallback Notes IDs, and reindex recovery.
3. **Next: read-only local AI interface.** Provide a small local search service for OpenClaw or a local LLM. Return bounded snippets with stable source IDs and paths. Add optional semantic retrieval only after measuring keyword-search gaps; keep embeddings and model execution local and opt-in.

## Run locally

Requires Python 3.10+ with an SQLite build that includes FTS5. From this directory:

```sh
PYTHONPATH=src python3 -B -m nex_knowledge.cli sync \
  --db data/knowledge.db \
  --notes-jsonl /path/to/apple-notes-pipeline/data/processed/notes.jsonl \
  --chatgpt-archive /path/to/chatgpt/archive \
  --markdown-dir /path/to/personal/markdown
PYTHONPATH=src python3 -B -m nex_knowledge.cli search --db data/knowledge.db 'garden AND plans'
PYTHONPATH=src python3 -B -m unittest discover -s tests
```

Each source argument can be repeated. Supply only sources you intend to index. The sync command never triggers a live export or browser capture. Search uses SQLite FTS5 query syntax and prints local snippets to the terminal. The `data/` directory and database files are ignored by Git; keep any other chosen database path outside public repositories. Do not commit or upload a populated database, exports, or search output.

An unchanged `(source, source_id)` with the same content and source path skips the write. The CLI reports added, updated, unchanged, and missing-but-preserved counts. This is projection-level incremental sync: upstream Notes export and ChatGPT capture have their own incremental rules. Empty or partial snapshots do not delete indexed records. The current CLI does not mark results stale, so inspect upstream validation before trusting coverage.

## Proposed commits

- `feat(knowledge): add local source adapters and incremental FTS5 search`
- `feat(knowledge): track source snapshots and explicit stale reconciliation`
- `feat(knowledge): expose bounded read-only local search service`
