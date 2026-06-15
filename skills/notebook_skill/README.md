# notebook_skill

Shared plain-text notebook for AdaOS Web UI.

The first version keeps note records in process memory and projects the selected
note into Yjs at `data/notebook/editor`. The note list is published through the
`notebook_skill.notes` stream. Only one note is selected for editing at a time.

Telegram export uses the root `/io/tg/send` outbox contract. Configure a paired
hub or set `TG_CHAT_ID` / `TELEGRAM_CHAT_ID`.
