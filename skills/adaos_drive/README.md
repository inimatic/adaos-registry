# AdaOS Drive

AdaOS Drive is a browser-facing, two-panel file manager inspired by Total Commander.
The first version connects only local folders that the user explicitly adds as
sources.

## Current scope

- Two independent file panels with a single active panel state.
- Source selector per panel and an add-source form for local folders.
- Connected local sources are shared across Drive webspaces; panel paths and
  selections remain webspace-local.
- Lazy tree loading through `expand_tree`; folders are scanned only when a branch
  is requested.
- File table columns: icon, name, extension, size, modified time.
- Upload into the current folder.
- Rename selected item.
- Copy selected file or folder to the other panel.
- Preview supported text, JSON, XML, Markdown, CSV, code and image formats.
- Create a Root-mediated guest browser link for a selected file without copying
  the file into skill storage.
- Download guest links through `/v1/drive/public-links/<public_token>/content`
  with HTTP range streaming.
- New folder creation and refresh commands.

## Safety model

All filesystem operations are constrained to the selected source root. Relative
paths are normalized and parent traversal, absolute paths, Windows drive
segments, path separators inside file names, and null bytes are rejected.

Guest links store a local Hub record for the selected source path and a Root
record for `public_token -> subnet_id/node/adaos_drive/hub_token`. The browser
URL contains `zone` and `public_token` only; Root resolves the subnet internally
and passes the Hub token over the existing route relay.

Current public access is token-scoped, readonly Drive access through the public
face `adaos_drive.files.public`. It is not a full `guest` webspace/home profile
yet. The public browser view uses the Root `/v1/drive/public-links/<token>/list`
and `/content` endpoints and keeps a browser-local public device id for
recipient-level statistics.
