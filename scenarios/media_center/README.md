# Media Center

Media Center is an MVP scenario for a production-grade media-center direction.
It intentionally starts with the correct boundary:

- core media plane owns storage publication, safe content paths, resource descriptors, and streaming;
- `media_center_skill` owns catalog state, filters, favorites, and playback planning;
- legacy `media_indexer_skill` can contribute resources only through SDK-normalized descriptors.

## MVP Workflow

1. Add one or more media folders. The scenario stores folder roots in `media_center_skill`.
2. Import folders. The skill publishes playable video/audio files through `adaos.sdk.io.media.publish_media_file`, then indexes the resulting Media Server descriptors.
3. Run **Refresh catalog** to reconcile existing `media_server` and compatibility `media_indexer` resources.
4. Browse the durable catalog by playable kind, source, sort order, or explicit text search.
5. Move through large libraries with server-backed `limit`/`offset` pages.
6. Select an item to inspect its resource descriptor and playback path.
7. Use the player surface to preview playable video/audio resources through core media routes.

The **Catalog** is intentionally broader than the configured **Media folders** list.
An empty folders list only means that no new import roots were configured through
Media Center yet. The catalog may still contain resources already known to the
core Media Server or compatibility `media_indexer_skill`.

Folder import actions are long-running skill calls. The scenario requests a
bounded 600 second timeout, and `media_center_skill` declares the same runtime
budget for `scan_roots` and `import_folder`. Skill-specific user-facing errors
are represented as structured machine codes plus `human_message_i18n` keys; the
Media Center dictionaries live in this scenario and in `media_center_skill/i18n`,
not in bundled core client translations.

## Deliberate Non-Goals For This Milestone

The MVP does not implement movie/episode metadata enrichment, recommendations,
watch queues, remote TV/player control, transcoding, subtitles, or rich media
library source policies. Those belong in the later production media-center
scenario and skills built on top of this catalog foundation.
