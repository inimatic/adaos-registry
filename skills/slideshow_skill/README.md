# slideshow_skill

Pilot skill for ReDevice endpoint surfaces.

The skill runs on a hub/member node and queues `display.render_surface`
commands for admitted ReDevice endpoints. It does not install UI on the
endpoint. The ReDevice agent receives concrete slideshow surface commands,
renders bounded cached JPEG thumbnails, and sends normalized surface events
such as `next` and `favorite_toggle` back to the root API.

Dashboard flow:

1. `Refresh endpoints` loads admitted ReDevice endpoints into the endpoint table.
2. `Add/remove` builds a multi-endpoint sync group; `Only` replaces the group.
3. `Preview photos` scans the source folder and shows the files that will be converted.
4. `Start` queues a fullscreen `display.render_surface` command for all selected endpoints.
5. Desktop widgets expose the current frame plus Prev, Next, and Fav controls.

Runtime state:

- favorites are global for this skill instance;
- mode is `sequential` or `random`;
- scope is `all` or `favorites`;
- sync mode broadcasts skill-selected state to all selected endpoints;
- endpoint payload is bounded to 10 current items plus up to 20 favorites.

Default photo source:

```text
C:\Users\Zver\Pictures
```

Override with `SLIDESHOW_SOURCE_DIR`.
