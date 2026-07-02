# notebook_skill

Shared plain-text notebook for AdaOS Web UI.

Notebook keeps note records in skill-local memory and projects the selected note
into Yjs at `data/desktop/notebook/editor`. The `desktop`, `desktop-dev`, and
`default` webspace ids are treated as aliases of one durable Notebook state so a
reload or runtime update cannot re-publish an older alias over a newer edit. The
note list is published through the `notebook_skill.notes` stream. Only one note
is selected for editing at a time.

Editor changes are auto-saved through the `save_note` skill action and then
republished to Yjs; there is no manual Save button.

Telegram export uses the root `/io/tg/send` outbox contract. Configure a paired
hub or set `TG_CHAT_ID` / `TELEGRAM_CHAT_ID`.
