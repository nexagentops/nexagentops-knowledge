# NexAgentOps Knowledge

A local, rebuildable keyword search index for existing personal knowledge exports. It uses only Python's standard library and SQLite FTS5. It does not access Apple Notes, a browser, a network service, or an LLM.

## Architecture review

| Existing source | Reusable boundary | Integration decision |
| --- | --- | --- |
| `apple-notes-pipeline` | Read-only JXA export, normalized `notes.jsonl`, stable source IDs when Notes supplies them, metadata-only diffs and count-drop validation | Consume current normalized JSONL. Do not run its exporter or index generated Markdown separately. Generated fallback IDs may change after edits. |
| `chatgpt-archive` | Canonical conversation JSON, stable canonical conversation ID, content-addressed revisions, rebuildable SQLite FTS5, bounded resumable capture | Consume canonical `raw/*.json`, never its operational database or browser session. Preserve partial-capture limitations from the source. |
| User Markdown | Plain files with filesystem path and modification time | Read only explicitly selected directories. Keep these separate from generated source Markdown. |

The unified SQLite database is a disposable projection. Existing JSONL, canonical conversation JSON, and Markdown files remain authoritative. Document identity is `(source, source_id)`; a later sync updates an existing row rather than creating another. The adapters reject duplicate IDs within and across selected inputs. Similar text from distinct source identities remains separate so attribution is not lost.

## First three contributions

1. **Implemented: local keyword search.** Source adapters, a transactional SQLite FTS5 index, incremental hash checks, attribution, timestamps, and a CLI. Synthetic tests cover the three source types, repeat syncs, updates, duplicate IDs, and missing-record preservation.
2. **Implemented: source snapshots and reconciliation.** Track each committed import run, selected source path, authority flag, counts, first and last seen times, and active/stale status. Missing records become stale only after an explicitly authoritative import of that same path.
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
PYTHONPATH=src python3 -B -m nex_knowledge.cli snapshots --db data/knowledge.db
PYTHONPATH=src python3 -B -m unittest discover -s tests
```

Each source argument can be repeated. Supply only sources you intend to index. The sync command never triggers a live export or browser capture. Search uses SQLite FTS5 query syntax and prints local snippets, source paths, provenance, timestamps, and `active`/`stale` status to the terminal. `snapshots` shows metadata-only recent import runs and their reconciliation counts. The `data/` directory and database files are ignored by Git; keep any other chosen database path outside public repositories. Do not commit or upload a populated database, exports, or search output.

## Snapshot and reconciliation semantics

Every `sync` creates a run ID and one snapshot entry for each selected source path. The selected JSONL file, archive directory, or Markdown directory is a separate **scope**. Counts distinguish added, updated, unchanged, reappeared, missing, and newly marked stale records. A reappearing record can also count as updated if its content changed. An unchanged record updates its last-seen metadata but skips FTS reindexing. A run is committed transactionally after all selected inputs have been read and validated.

Imports are **non-authoritative by default**. They record missing counts while preserving existing active/stale status. To assert that an input is a complete snapshot of that particular source path, use one of these explicit alternatives:

```sh
PYTHONPATH=src python3 -B -m nex_knowledge.cli sync --db data/knowledge.db \
  --authoritative-notes-jsonl /path/to/validated/notes.jsonl
PYTHONPATH=src python3 -B -m nex_knowledge.cli sync --db data/knowledge.db \
  --authoritative-chatgpt-archive /path/to/complete/local/archive
PYTHONPATH=src python3 -B -m nex_knowledge.cli sync --db data/knowledge.db \
  --authoritative-markdown-dir /path/to/fully/readable/markdown
```

Use the authoritative flag only after confirming that the selected local input is complete for that path. For Notes, upstream count-drop validation matters. For ChatGPT, authority refers to the **local archive's canonical raw files**, not all conversations that might exist remotely. A temporary sync gap or incomplete upstream capture must remain non-authoritative. A missing record is an observation about one snapshot; it does **not** prove deletion. Authoritative absence changes `status` to `stale` and retains content and searchability. Reimporting the record changes it back to `active`. No default operation physically deletes indexed records.

The database adds `scope`, `status`, `first_seen_at`, `last_seen_at`, and `last_seen_run_id` to documents, plus `sync_runs` and `source_snapshots` tables. Existing indexes are upgraded in place. Pre-upgrade rows receive a legacy empty scope and remain active; a subsequent import assigns seen rows to the selected source path. Unseen legacy rows are deliberately not marked stale automatically. If a source ID appears under a different nonlegacy scope, sync fails rather than silently changing ownership. Back up a populated local database before relying on a new version; it is rebuildable from source exports.

This is projection-level incremental sync: upstream Notes export and ChatGPT capture retain their own incremental rules. The index does not infer cross-source duplicates from similar content, and Apple Notes generated fallback IDs can change after edits.

## Proposed commits

- `feat(knowledge): add local source adapters and incremental FTS5 search`
- `feat(knowledge): track source snapshots and explicit stale reconciliation`
- `feat(knowledge): expose bounded read-only local search service`
