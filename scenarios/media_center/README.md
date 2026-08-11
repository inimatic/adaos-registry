# Media Center

Media Center is an MVP scenario for a production-grade media-center direction.
It intentionally starts with the correct boundary:

- core media plane owns storage publication, safe content paths, resource descriptors, and streaming;
- `media_center_skill` owns catalog state, filters, favorites, and playback planning;
- legacy `media_indexer_skill` can contribute resources only through SDK-normalized descriptors.

## MVP Workflow

1. Run **Refresh catalog** to reconcile `media_server` and compatibility `media_indexer` resources.
2. Browse the durable catalog by kind, source, sort order, or text query.
3. Select an item to inspect its resource descriptor and playback path.
4. Use the player surface to preview playable video/audio resources through core media routes.

## Deliberate Non-Goals For This Milestone

The MVP does not implement movie/episode metadata enrichment, recommendations,
watch queues, remote TV/player control, transcoding, subtitles, or full media
library source management. Those belong in the later production media-center
scenario and skills built on top of this catalog foundation.

