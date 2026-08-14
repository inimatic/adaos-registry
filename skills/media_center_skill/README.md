# media_center_skill

`media_center_skill` owns Media Center catalog state only: durable rows, folder
roots, favorites, filters, and playback plans derived from core media resource
descriptors.

The core media plane remains responsible for media registration and playback
routes. Folder imports call `adaos.sdk.io.media.register_media_file`, then index
the returned `adaos.media.resource.v1` descriptors. Registration stores only a
root-bound reference in `.adaos`; media bytes stay at their original path.
Catalog migration retires pre-reference `media-center-*-import.*` resources so
playback cannot silently fall back to an old managed copy. Existing legacy bytes
are left untouched for an explicit, separately reviewed cleanup operation.

The default `library()` projection is `media_kind="playable"` so the main Media
Center catalog stays focused on video/audio resources supported by the current
Media Server. Image descriptors remain queryable only when a caller explicitly
requests `media_kind="image"`.

`playback_queue()` always returns the selected item first and clamps the entire
queue to ten rows. This is the server-side budget used by the modal player and
prevents the browser playlist control from loading a catalog page.

## User-Facing Errors

Tools return stable machine codes in `error`/`code` and may include
`human_message_i18n` for UI presentation. Media Center translations live in
`i18n/*.json` and are exported by the skill's `webui.json` resource catalog,
not in the scenario or bundled core client translations.

Example:

```json
{
  "ok": false,
  "error": "no_active_media_roots",
  "human_message_i18n": {
    "key": "runtime.media_center.error.no_active_media_roots"
  }
}
```

The direct `human_message` field is a fallback for clients that have not loaded
runtime dictionaries yet.
