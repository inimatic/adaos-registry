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
3. `Apply source` stores the source folder in AdaOS skill memory.
4. `Refresh index` starts a background SQLite index refresh for the source tree.
5. `Folders` opens a separate folder picker backed by the indexed source tree.
6. `Preview photos` shows indexed files in the current folder/scope.
7. `Play` starts endpoint autoplay and sends the current cache window.
8. `Stop` clears the active ReDevice slideshow surface.
9. Desktop widgets expose the current frame plus Play, Stop, Prev, Next, and Fav controls.

Large libraries:

- indexing runs in the runtime process background and continues after the modal is closed;
- the `Photo index` tile shows running/completed/failed state plus indexed and visited counts;
- a refresh keeps old rows and favorite flags until a full scan completes;
- if a scan is canceled or interrupted, existing and partial rows remain usable;
- folder output is capped for WebIO, with the full source still kept in SQLite;
- endpoint refresh and playback controls read the existing index and do not rescan the file tree.

Runtime state:

- skill state is stored through AdaOS skill memory, not in ad-hoc user-home files;
- generated thumbnails are stored beside source photos in `.adaos-thumbs`;
- the persistent photo index is stored under the skill runtime data area;
- favorites are global for this skill instance and persisted in the photo index;
- mode is `sequential` or `random`;
- scope is `all` or `favorites`;
- display mode is `fit` or `crop`;
- sync mode broadcasts skill-selected state to all selected endpoints;
- when running in sync mode, the first selected endpoint drives autoplay;
- endpoint payload targets 4 current items, but root inline fallback is budget guarded;
- when inline transport is selected, the endpoint receives as many compact cached frames as fit the command budget;
- a future `content_url` pull route should provide the guaranteed 10-frame cache without inflating root commands.

Service behavior:

- the skill resumes its ReDevice event poller on `sys.ready` when it has selected endpoints or a running session;
- endpoint-side `next` events advance the skill's cache window;
- in synchronous mode, endpoint-side `next` refreshes the shared window on all selected endpoints;
- in independent mode, endpoint-side `next` refreshes only the endpoint that produced the event.

Default photo source:

```text
C:\Users\Zver\Pictures
```

Override with `SLIDESHOW_SOURCE_DIR`.
