# slideshow_skill

Pilot skill for ReDevice endpoint surfaces.

The skill runs on a hub/member node and queues a `display.render_surface`
command for an admitted ReDevice endpoint. It does not install UI on the
endpoint. The ReDevice agent receives a concrete slideshow surface command,
renders cached JPEG thumbnails, and sends normalized surface events such as
`next` and `favorite_toggle` back to the root API.

Default photo source:

```text
C:\Users\Zver\Pictures
```

Override with `SLIDESHOW_SOURCE_DIR`.
