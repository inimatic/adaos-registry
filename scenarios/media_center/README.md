# Media Center

Media Center is an MVP scenario for a production-grade media-center direction.
It intentionally starts with the correct boundary:

- core media plane owns reference registration, safe content paths, resource descriptors, and streaming;
- `media_center_skill` owns catalog state, filters, favorites, and playback planning;
- legacy `media_indexer_skill` can contribute resources only through SDK-normalized descriptors.

## MVP Workflow

1. Open **Settings** and add one or more media folders.
2. The skill registers playable video/audio files through
   `adaos.sdk.io.media.register_media_file`; files remain at their original
   paths and `.adaos` stores only catalog/reference metadata.
   Legacy managed imports are hidden from discovery and retired from the active
   catalog; their existing bytes are not deleted automatically.
3. Use **Refresh catalog** in Settings to reconcile existing `media_server` and
   compatibility `media_indexer` resources.
4. The main surface contains only playable type filters, explicit text search,
   a catalog, and page navigation. Source and sort options live in Settings.
5. The catalog requests 30 rows per server-backed `limit`/`offset` page.
6. Select a row to open playback immediately in a modal.
7. The modal loads a server-bounded queue of at most ten items and streams the
   selected original file through core ranged media routes.

The **Catalog** is intentionally broader than the configured **Media folders** list.
An empty folders list only means that no new import roots were configured through
Media Center yet. The catalog may still contain resources already known to the
core Media Server or compatibility `media_indexer_skill`.
The primary library view requests `playable` media by default, so image
descriptors from slideshow/legacy sources do not crowd the playback catalog.

Folder registration actions are long-running skill calls. The scenario requests a
bounded 600 second timeout, and `media_center_skill` declares the same runtime
budget for `scan_roots` and `import_folder`. Skill-specific user-facing errors
are represented as structured machine codes plus `human_message_i18n` keys; the
Media Center dictionaries are declared by `media_center_skill` and live in
`media_center_skill/i18n`, not in the scenario or bundled core client
translations.

The playlist and catalog limits are enforced on both sides of the UI boundary:
the scenario requests 30 catalog rows and ten playback rows, the skill clamps
the playback queue to ten, and the universal client player clamps its dropdown
before rendering.

## Deliberate Non-Goals For This Milestone

The MVP does not implement movie/episode metadata enrichment, recommendations,
watch queues, remote TV/player control, transcoding, subtitles, or rich media
library source policies. Those belong in the later production media-center
scenario and skills built on top of this catalog foundation.
