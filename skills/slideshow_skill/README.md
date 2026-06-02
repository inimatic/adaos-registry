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
3. `Refresh index` rebuilds a persistent SQLite index for the source tree.
4. `Folders` selects a top-level folder from the indexed source tree.
5. `Preview photos` shows indexed files in the current folder/scope.
6. `Run` switches between `Running` and `Stopped`.
7. Desktop widgets expose the current frame plus Prev, Next, and Fav controls.

Runtime state:

- skill state is stored through AdaOS skill memory, not in ad-hoc user-home files;
- generated thumbnails are stored beside source photos in `.adaos-thumbs`;
- favorites are global for this skill instance and persisted in the photo index;
- mode is `sequential` or `random`;
- scope is `all` or `favorites`;
- display mode is `fit` or `crop`;
- sync mode broadcasts skill-selected state to all selected endpoints;
- when running in sync mode, the first selected endpoint drives autoplay;
- endpoint payload is bounded to 10 current items plus up to 20 favorites.

Default photo source:

```text
C:\Users\Zver\Pictures
```

Override with `SLIDESHOW_SOURCE_DIR`.
