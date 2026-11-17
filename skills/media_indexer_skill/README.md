# media_indexer_skill

AdaOS skill for local semantic search over media files.

The skill scans a user-selected directory, extracts lightweight technical
metadata, enriches media metadata where possible, and builds a FAISS index for
natural-language search across videos, audio files, and images.

## Data Routes

- `data/media_indexer/status` and `data/media_indexer/form` are compact Yjs
  projection state for first paint and reconnect recovery.
- `data/media_indexer/results` is a bounded Yjs result list. It is capped by the
  handler and should not be used for large diagnostics.
- `media_indexer.operations` is a replace-mode WebIO stream receiver for live
  operation status. The skill republishes the last operation on
  `webio.stream.snapshot.requested`.
- Private durable settings live in skill memory under `media_indexer.settings`.
- Durable metadata lives in the skill runtime DB under
  `data/db/media_indexer/manifest.json` and `data/db/media_indexer/library.json`.
  Library records include SHA-256 fingerprints, file paths, extracted technical
  metadata, NER fields, enrichment payloads, and index text.
- The FAISS index is a derived cache stored under `data/internal/media_indexer/faiss`.
  It can be rebuilt from the library and source files; its summary is mirrored
  in skill memory under `media_indexer.index`.

## Model Storage

The NER weights are declared in `skill.yaml` under
`models.artifacts.weights`. During install AdaOS places the active artifact in
the skill-owned runtime data store:

```text
data/files/models/model2.pt
```

For the current bootstrap period the upload source is a git-ignored local file
at `ml/weights/model2.pt`, seeded from Google Drive file
`19YBXzTYLoizbm8RF8gigUQ0fApZVmpoZ`. `skill push` uploads the file to Root only
when its SHA-256 differs from Root `current`; Root keeps `previous` for
rollback. Runtime code uses `data/files/models` first and falls back to the
legacy local/Google Drive path only for development recovery.

## Tools

- `scan_and_index(directory)` scans and indexes a directory.
- `search_media(query, k=5)` searches the persisted/in-memory index.
- `get_settings()` returns persisted settings, index metadata, and model status.
- `rehydrate()` restores lightweight settings without loading ML models.
- `dispose()` releases in-memory models and indexes.

## Runtime Notes

Importing the handler is passive. Heavy packages and models are loaded lazily
only when indexing or searching requires them. Smoke tests intentionally avoid
loading the ML stack.
