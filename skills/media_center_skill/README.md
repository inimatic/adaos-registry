# media_center_skill

`media_center_skill` owns Media Center catalog state only: durable rows, folder
roots, favorites, filters, and playback plans derived from core media resource
descriptors.

The core media plane remains responsible for media publication and playback
routes. Folder imports call `adaos.sdk.io.media.publish_media_file`, then index
the returned `adaos.media.resource.v1` descriptors.

## User-Facing Errors

Tools return stable machine codes in `error`/`code` and may include
`human_message_i18n` for UI presentation. Media Center translations live in
`i18n/*.json` and in the scenario browser-resource dictionaries, not in bundled
core client translations.

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
