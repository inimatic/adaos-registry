# notebook_skill

Shared plain-text notebook for AdaOS Web UI.

Notebook keeps note records in skill-local memory. The `desktop`, `desktop-dev`,
and `default` webspace ids are treated as aliases of one durable Notebook state
so a reload or runtime update cannot re-publish an older alias over a newer edit.
The bounded `notebook_skill.notes`, `notebook_skill.editor`, and
`notebook_skill.latest` streams independently publish the note list, selected
editor value, and desktop card. Only one note is selected for editing at a time.

Editor changes are auto-saved through the `save_note` skill action and then
republished to the three receiver-specific read models; there is no manual Save
button. Snapshot-on-subscribe reloads durable state and publishes only the
receiver requested by the browser.

Telegram export uses the root `/io/tg/send` outbox contract. Configure a paired
hub or set `TG_CHAT_ID` / `TELEGRAM_CHAT_ID`.
