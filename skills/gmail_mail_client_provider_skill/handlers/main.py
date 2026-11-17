"""Application-local Gmail adapter. No credentials or mail content are persisted."""
from __future__ import annotations

import base64
import binascii
from contextlib import closing
from email.message import EmailMessage
from email import policy
from email.utils import getaddresses
from html.parser import HTMLParser
import hashlib
import re
import sqlite3
import uuid

from adaos.sdk import access
from adaos.sdk.core.decorators import tool
from adaos.sdk.data.lifecycle import ensure_database
from adaos.sdk.data.skill_env import skill_data_root
from adaos.sdk.providers import gmail

ACCOUNT = "google.gmail"
DATABASE = "send_commands.sqlite3"
MAX_BODY = 50_000


def lang_res():
    return {}


def _authorize(write=False):
    access.require("workspace.write" if write else "workspace.read")
    access.require("providers.google.gmail")
    context = access.application()
    if not context or not context.get("application_id") or not context.get("subject_ref") or not access.caller():
        raise PermissionError("application_context_missing")
    return context


def _error(exc):
    status = {"gmail_account_not_connected": "disconnected", "gmail_reconnect_required": "expired",
              "gmail_permission_denied": "permission_denied", "gmail_required_scope_missing": "permission_denied"}.get(exc.code, "error")
    return {"ok": False, "error": exc.code, "status": status,
            "message": "Gmail operation was not confirmed.", "retryable": exc.retryable}


def _read_unavailable(exc, **empty):
    """Return an authoritative empty read while account setup is incomplete."""
    result = _error(exc)
    if result["status"] == "error":
        return result
    return {"ok": True, "status": result["status"],
            "message": "Connect Gmail to load mail.", **empty}


def _result(envelope, operation):
    if (not isinstance(envelope, dict) or envelope.get("ok") is not True
            or envelope.get("provider_id") != ACCOUNT or envelope.get("operation") != operation
            or not isinstance(envelope.get("result"), dict)):
        raise ValueError("invalid_provider_response")
    return envelope["result"]


def _text(value, limit=1024):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("invalid_text")
    return value


def _id(value):
    value = _text(value, 256)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid_id")
    return value


def _confirmation(result):
    labels = result.get("labelIds", [])
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise ValueError("invalid_provider_labels")
    return {"id": _id(result.get("id")), "threadId": _id(result.get("threadId")), "labelIds": [_id(x) for x in labels[:100]]}


class _PlainHTML(HTMLParser):
    """Extract inert text without rendering HTML or fetching remote resources."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'head'):
            self.hidden += 1
        if tag in ('p', 'br', 'div', 'li') and not self.hidden:
            self.chunks.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'head'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.chunks.append(data)


def _body(part, depth=0):
    if depth > 20 or not isinstance(part, dict) or part.get("filename"):
        return ""
    if part.get("mimeType") in ("text/plain", "text/html"):
        data = part.get("body", {}).get("data", "")
        if not isinstance(data, str) or len(data) > MAX_BODY * 2:
            return "[Message body exceeds reader limit]"
        try:
            raw = base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)
            content_type = next((h.get('value', '') for h in part.get('headers', [])
                                 if h.get('name', '').lower() == 'content-type'), '')
            charset = re.search(r'charset=["\']?([\w-]+)', content_type, re.I)
            try:
                text = raw[:MAX_BODY].decode(charset.group(1) if charset else 'utf-8', errors='replace')
            except LookupError:
                text = raw[:MAX_BODY].decode('utf-8', errors='replace')
            if part.get('mimeType') == 'text/html':
                parser = _PlainHTML()
                parser.feed(text)
                return ''.join(parser.chunks).strip()[:MAX_BODY]
            return text
        except (ValueError, binascii.Error):
            return "[Message body could not be decoded]"
    parts = part.get("parts", [])
    if not isinstance(parts, list):
        return ""
    if part.get('mimeType') == 'multipart/alternative':
        plain = next((p for p in parts if p.get('mimeType') == 'text/plain'), None)
        if plain is not None:
            return _body(plain, depth + 1)
    return "\n".join(filter(None, (_body(p, depth + 1) for p in parts[:100])))[:MAX_BODY]


def _message(result, body=False):
    item = _confirmation(result)
    payload = result.get("payload", {})
    headers = {str(h.get("name", "")).lower(): str(h.get("value", ""))[:2048]
               for h in payload.get("headers", []) if isinstance(h, dict)}
    item.update({"subject": headers.get("subject", "(No subject)"), "from": headers.get("from", ""),
                 "date": headers.get("date", ""), "snippet": str(result.get("snippet", ""))[:1024],
                 "isUnread": "UNREAD" in item["labelIds"], "isStarred": "STARRED" in item["labelIds"], "icon": "mail"})
    if body:
        item["body"] = _body(payload) or "[No inline plain-text body; open Gmail to view this message]"
    return item


@tool
def connection_status():
    _authorize()
    try:
        value = gmail.connection_status(account_id=ACCOUNT)
        if value.get("ok") is not True or value.get("status") != "connected":
            raise ValueError("invalid_provider_response")
        return {"ok": True, "status": "connected", "email_address": _text(value.get("email_address"))}
    except gmail.GmailProviderError as exc:
        return _read_unavailable(exc, email_address="")


@tool
def begin_connection():
    _authorize(True)
    try:
        value = gmail.begin_connection(account_id=ACCOUNT)
        if value.get('ok') is not True:
            raise ValueError('invalid_provider_response')
        return {"ok": True, "authorization_url": value["authorization_url"], "expires_at": value["expires_at"]}
    except gmail.GmailProviderError as exc:
        return _error(exc)


@tool
def list_messages(query="", tab="inbox", unread=False, starred=False, label="", page_token=""):
    _authorize()
    query, page_token = _text(query, 2048), _text(page_token, 2048)
    folders = {"inbox": "INBOX", "starred": "STARRED", "sent": "SENT", "trash": "TRASH"}
    if tab not in folders:
        raise ValueError("invalid_folder")
    labels = [folders[tab]]
    if unread is True:
        labels.append("UNREAD")
    if starred is True:
        labels.append("STARRED")
    if label:
        labels.append(_id(label))
    try:
        result = _result(gmail.list_messages(query=query, label_ids=list(dict.fromkeys(labels)),
                         page_token=page_token, max_results=25, account_id=ACCOUNT), "list_messages")
        rows = result.get("messages", [])
        if not isinstance(rows, list) or len(rows) > 25:
            raise ValueError("invalid_provider_page")
        items = [_message(_result(gmail.get_message(_id(row.get("id")), format="metadata", account_id=ACCOUNT),
                                 "get_message")) for row in rows]
        token = _text(result.get("nextPageToken", ""), 2048)
        return {"ok": True, "items": items, "nextPageToken": token,
                "pagination": [{"id": "page", "nextPageToken": token,
                                "summary": "More messages available" if token else "End of results"}]}
    except gmail.GmailProviderError as exc:
        return _read_unavailable(exc, items=[], nextPageToken="", pagination=[])


@tool
def get_message(id=""):
    _authorize()
    if not id:
        return {"ok": True, "item": {}}
    try:
        return {"ok": True, "item": _message(_result(gmail.get_message(_id(id), format="full", account_id=ACCOUNT),
                                                    "get_message"), body=True)}
    except gmail.GmailProviderError as exc:
        return _read_unavailable(exc, item={})


@tool
def list_labels():
    _authorize()
    try:
        result = _result(gmail.list_labels(account_id=ACCOUNT), "list_labels")
        return {"ok": True, "items": [{"id": _id(x.get("id")), "name": _text(x.get("name"), 256)}
                                      for x in result.get("labels", [])[:500]]}
    except gmail.GmailProviderError as exc:
        return _read_unavailable(exc, items=[])


@tool
def mutate_message(id, action, label=""):
    _authorize(True)
    id = _id(id)
    changes = {"archive": ([], ["INBOX"]), "mark_read": ([], ["UNREAD"]),
               "mark_unread": (["UNREAD"], []), "star": (["STARRED"], []), "unstar": ([], ["STARRED"])}
    if action in ("label", "remove_label"):
        changes[action] = ([_id(label)], []) if action == "label" else ([], [_id(label)])
    if action not in changes and action != "trash":
        raise ValueError("invalid_action")
    try:
        if action == "trash":
            result = _result(gmail.trash_message(id, account_id=ACCOUNT), "trash_message")
        else:
            add, remove = changes[action]
            result = _result(gmail.modify_message(id, add_label_ids=add, remove_label_ids=remove,
                             account_id=ACCOUNT), "modify_message")
        return {"ok": True, "item": _confirmation(result)}
    except gmail.GmailProviderError as exc:
        return _error(exc)


@tool
def prepare_send():
    _authorize(True)
    return {"ok": True, "command_id": str(uuid.uuid4())}


def _scope(context):
    return hashlib.sha256((context["application_id"] + "\0" + context["subject_ref"] + "\0" + ACCOUNT).encode()).hexdigest()


@tool
def send_message(command_id, values):
    context = _authorize(True)
    command_id = str(uuid.UUID(command_id))
    if not isinstance(values, dict):
        raise ValueError("invalid_message")
    to, subject, body = (_text(values.get(k, ""), limit) for k, limit in
                         (("to", 2048), ("subject", 998), ("body", MAX_BODY)))
    if any(c in to + subject for c in "\r\n") or not to.strip():
        raise ValueError("invalid_headers")
    addresses = getaddresses([to])
    if not addresses or any(not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", addr) for _, addr in addresses):
        raise ValueError("invalid_recipient")
    message = EmailMessage(policy=policy.SMTP)
    message["To"], message["Subject"] = to, subject
    message.set_content(body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
    scope = _scope(context)
    ensure_database(DATABASE)
    with closing(sqlite3.connect(skill_data_root() / DATABASE, timeout=10)) as db:
        with db:
            inserted = db.execute("INSERT OR IGNORE INTO send_commands(scope,command_id,status) VALUES (?,?,'unknown')",
                                  (scope, command_id)).rowcount
        if not inserted:
            status, message_id = db.execute("SELECT status,message_id FROM send_commands WHERE scope=? AND command_id=?",
                                            (scope, command_id)).fetchone()
            return {"ok": status == "confirmed", "delivery_status": status, "id": message_id or "",
                    "duplicate": True, "message": "Check Sent in Gmail before starting another send."}
        # Commit reservation BEFORE the single SDK call, including ambiguous outcomes.
        try:
            result = _confirmation(_result(gmail.send_message(raw, idempotency_key=scope + ":" + command_id,
                                                              account_id=ACCOUNT), "send_message"))
        except (gmail.GmailProviderError, ValueError, TypeError, KeyError):
            return {"ok": False, "delivery_status": "unknown", "error": "send_unconfirmed",
                    "retryable": False, "message": "Check Sent in Gmail; this command will not be sent again."}
        with db:
            db.execute("UPDATE send_commands SET status='confirmed',message_id=? WHERE scope=? AND command_id=?",
                       (result["id"], scope, command_id))
        return {"ok": True, "delivery_status": "confirmed", "id": result["id"]}
