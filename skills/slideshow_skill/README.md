# slideshow_skill

Pilot skill for ReDevice endpoint surfaces.

The skill runs on a hub/member node and queues a `display.render_surface`
command for an admitted ReDevice endpoint. It does not install UI on the
endpoint. The ReDevice agent receives a concrete slideshow surface command,
renders cached JPEG thumbnails, and sends normalized surface events such as
`next` and `favorite_toggle` back to the root API.

Dashboard flow:

1. `Refresh endpoints` loads admitted ReDevice endpoints into the endpoint table.
2. `Use` marks the endpoint selected and sets the target for `Start slideshow`.
3. `Preview photos` scans the source folder and shows the files that will be converted.
4. `Start slideshow` queues a `display.render_surface` command for the selected endpoint.

Default photo source:

```text
C:\Users\Zver\Pictures
```

Override with `SLIDESHOW_SOURCE_DIR`.
