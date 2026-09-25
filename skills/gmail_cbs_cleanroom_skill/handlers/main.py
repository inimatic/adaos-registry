"""Transient mailbox projections. Core owns identity, credentials and provider IO."""
import base64
from contextlib import closing
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import getaddresses
import hashlib
from html.parser import HTMLParser
import re
import sqlite3
import uuid
from adaos.sdk import access
from adaos.sdk.core.decorators import tool
from adaos.sdk.data import skill_data_root
from adaos.sdk.data.lifecycle import ensure_database
from adaos.sdk.providers import gmail

DATABASE = "send_commands.sqlite3"
ERRORS = {
    "gmail_account_not_connected": "Connect your Gmail account to continue.",
    "gmail_reconnect_required": "Authorization expired or was revoked. Reconnect your account.",
    "gmail_permission_denied": "Access to this account was denied.",
    "gmail_required_scope_missing": "Reconnect and approve the required mail permission.",
    "gmail_provider_unavailable": "Gmail is unavailable or you are offline. Refresh to retry reads.",
    "google_oauth_not_configured": "Complete Google provider setup in Application settings.",
    "gmail_reusable_account_not_found": "No existing Gmail connection is available for this user.",
    "gmail_send_unconfirmed": "Delivery is unconfirmed. Check Sent mail before composing again; this command will not retry.",
    "invalid_input": "Check the message fields and try again.",
}


def _error(code):
    code = code if code in ERRORS else "gmail_provider_unavailable"
    return {"ok": False, "error": code, "message": ERRORS[code]}


def _authorize(write=False):
    access.require("workspace.write" if write else "workspace.read")
    access.require("providers.google.gmail")
    context = access.application()
    if not access.caller() or not context or not context.get("subject_ref"):
        raise PermissionError("application_context_missing")
    return context


def _call(operation, **kwargs):
    response = getattr(gmail, operation)(account_id="google.gmail", **kwargs)
    if response.get("ok") is not True:
        raise ValueError("invalid_provider_response")
    if operation not in (
        "connection_status",
        "begin_connection",
        "reusable_connections",
        "attach_reusable_connection",
    ):
        if response.get("operation") != operation or not isinstance(response.get("result"), dict):
            raise ValueError("invalid_provider_response")
        return response["result"]
    return response


def _bounded_call(fn):
    try:
        return fn()
    except gmail.GmailProviderError as exc:
        return _error(exc.code)
    except Exception:
        # No remote exception text, response body or request arguments may escape.
        return _error("gmail_provider_unavailable")


def _read_call(fn, account=False):
    """Project expected provider lifecycle states without hiding other failures."""
    states = {
        "gmail_account_not_connected": "disconnected",
        "gmail_reconnect_required": "auth_required",
        "gmail_permission_denied": "permission_denied",
        "gmail_required_scope_missing": "permission_denied",
        "gmail_provider_unavailable": "offline",
        "google_oauth_not_configured": "provider_error",
    }

    def read():
        try:
            return fn()
        except gmail.GmailProviderError as exc:
            if exc.code not in states:
                raise
            status, notice = states[exc.code], ERRORS[exc.code]
            if account:
                return {"ok": True, "items": [{"id": "google.gmail", "name": "Gmail",
                        "status": status, "least_privilege_note": notice}]}
            return {"ok": True, "items": [], "status": status, "notice": notice}

    return _bounded_call(read)


def _text(value, limit):
    return value[:limit] if isinstance(value, str) else ""


class _MailText(HTMLParser):
    """Extract inert text, dropping active content; never render provider HTML."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('br', 'p', 'div', 'li') and not self.hidden:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _body(part, depth=0):
    if depth > 12 or part.get("filename"):
        return ""
    if part.get("mimeType") in ("text/plain", "text/html"):
        data = _text(part.get("body", {}).get("data"), 90000)
        text = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
        if part.get('mimeType') == 'text/html':
            parser = _MailText()
            parser.feed(text)
            text = ''.join(parser.parts)
        return text[:16000]
    if part.get('mimeType') == 'multipart/alternative':
        parts = part.get('parts', [])[:30]
        plain = next((p for p in parts if p.get('mimeType') == 'text/plain'), None)
        return _body(plain if plain is not None else (parts[-1] if parts else {}), depth + 1)
    return "\n".join(_body(p, depth + 1) for p in part.get("parts", [])[:30])[:16000]


def _message(raw, body=False):
    payload = raw.get("payload", {})
    headers = {h["name"].lower(): _text(h.get("value"), 1000) for h in payload.get("headers", [])[:100]}
    labels = [_text(x, 128) for x in raw.get("labelIds", [])[:100]]
    return {"id": _text(raw["id"], 256), "thread_id": _text(raw["threadId"], 256),
            "from": headers.get("from", ""), "date": headers.get("date", ""),
            "Message": {"subject": headers.get("subject", ""), "to": headers.get("to", ""),
                        "body": _body(payload) if body else ""},
            "labels_ref": labels[0] if labels else "", "labels": labels,
            "unread": "UNREAD" in labels, "starred": "STARRED" in labels}


@tool(summary="Read account readiness.", side_effects="none")
def connection_status(id=""):
    _authorize()
    def read():
        response = _call("connection_status")
        if response.get('status') != 'connected':
            raise ValueError('invalid_provider_status')
        return {"ok": True, "items": [{"id": "google.gmail", "name": _text(response.get("email_address"), 320),
                "status": "connected", "least_privilege_note": "Gmail modify access permits reading, organizing and sending mail. No permanent deletion or label administration is requested."}]}
    return _read_call(read, account=True)


@tool(summary="Begin Core-owned OAuth connection.", side_effects="external_write")
def begin_connection():
    _authorize(True)
    return _bounded_call(lambda: {k: v for k, v in _call("begin_connection").items()
                                 if k in ("ok", "authorization_url", "expires_at")})


@tool(summary="List redacted Gmail connections available for explicit reuse.", side_effects="none")
def reusable_connections():
    _authorize()

    def read():
        response = _call("reusable_connections")
        items = []
        for account in response.get("accounts", [])[:10]:
            items.append({
                "id": _text(account.get("account_id"), 256),
                "email_address": _text(account.get("email_address"), 320),
                "status": _text(account.get("status"), 40),
                "attached": bool(account.get("attached")),
            })
        return {"ok": True, "items": items}

    return _bounded_call(read)


@tool(summary="Explicitly attach an existing Core-owned Gmail connection.", side_effects="local_write")
def attach_reusable_connection(account_id="google.gmail"):
    _authorize(True)
    if account_id != "google.gmail":
        return _error("invalid_input")

    def attach():
        response = _call("attach_reusable_connection")
        return {
            "ok": True,
            "id": _text(response.get("account_id"), 256),
            "email_address": _text(response.get("email_address"), 320),
            "status": _text(response.get("status"), 40),
            "attached": bool(response.get("attached")),
            "reused_credential": bool(response.get("reused_credential")),
        }

    return _bounded_call(attach)


@tool(summary="List provider labels.", side_effects="none")
def list_labels():
    _authorize()
    return _read_call(lambda: {"ok": True, "items": [{"id": _text(x["id"], 128), "title": _text(x["name"], 256)}
                            for x in _call("list_labels").get("labels", [])[:200]]})


@tool(summary="Search provider messages.", side_effects="none")
def list_messages(query="", label_id="INBOX", unread="", starred="", page_token=""):
    _authorize()
    if not isinstance(query, str) or len(query) > 2000 or unread not in ("", True, False) or starred not in ("", True, False):
        return _error("invalid_input")
    def read():
        filters = [("is:unread" if unread else "-is:unread")] if unread != "" else []
        if starred != "":
            filters.append("is:starred" if starred else "-is:starred")
        search = " ".join((["(" + query + ")"] if query else []) + filters)
        result = _call("list_message_summaries", query=search, label_ids=[label_id] if label_id else [],
                       page_token=page_token, max_results=20)
        items = [_message(m) for m in result.get("messages", [])[:20]]
        return {"ok": True, "items": items, "next_page_token": _text(result.get("nextPageToken"), 2048)}
    return _read_call(read)


@tool(summary="Read one transient message.", side_effects="none")
def get_message(id=""):
    _authorize()
    if not id:
        return {"ok": True, "items": []}
    return _read_call(lambda: {"ok": True, "items": [_message(_call("get_message", message_id=id, format="full"), True)]})


@tool(summary="Apply explicit mailbox changes.", side_effects="external_write")
def mutate_message(id, action, value=None):
    _authorize(True)
    if not id or action not in ("archive", "trash", "unread", "starred", "label"):
        return _error("invalid_input")
    if action in ("unread", "starred") and type(value) is not bool:
        return _error("invalid_input")
    if action == "label" and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)):
        return _error("invalid_input")
    def change():
        if action == "trash":
            raw = _call("trash_message", message_id=id)
        else:
            label = {"archive": "INBOX", "unread": "UNREAD", "starred": "STARRED"}.get(action, value)
            add = action == "label" or (action != "archive" and value)
            raw = _call("modify_message", message_id=id, add_label_ids=[label] if add else [], remove_label_ids=[] if add else [label])
        return {"ok": True, "id": _text(raw["id"], 256), "labels": [_text(x, 128) for x in raw.get("labelIds", [])[:100]]}
    return _bounded_call(change)


def _database():
    ensure_database(DATABASE)
    return sqlite3.connect(skill_data_root() / DATABASE, timeout=15)


def _scope(context):
    return hashlib.sha256((context["application_id"] + "\0" + context["subject_ref"]).encode()).hexdigest()


@tool(summary="Issue a durable send command without storing content.", side_effects="local_write")
def prepare_send():
    context = _authorize(True)
    command = str(uuid.uuid4())
    with closing(_database()) as db, db:
        db.execute("INSERT INTO send_commands(command_id, scope, status) VALUES (?, ?, 'ready')", (command, _scope(context)))
    return {"ok": True, "command_id": command}


@tool(summary="Send once after explicit user confirmation.", side_effects="external_write")
def send_message(command_id, to, subject, body):
    context = _authorize(True)
    if any(not isinstance(v, str) for v in (command_id, to, subject, body)):
        return _error("invalid_input")
    if not to or len(to) > 2000 or not subject or len(subject) > 998 or not body or len(body) > 50000 or any(c in to + subject for c in "\r\n\x00"):
        return _error("invalid_input")
    addresses = getaddresses([to])
    if not addresses or len(addresses) > 20 or any(not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", addr) for _, addr in addresses):
        return _error("invalid_input")
    msg = EmailMessage(policy=SMTP)
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
    scope = _scope(context)
    with closing(_database()) as db:
        with db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status, message_id FROM send_commands WHERE command_id=? AND scope=?", (command_id, scope)).fetchone()
            if row is None:
                return _error("invalid_input")
            if row[0] == "sent":
                return {"ok": True, "id": row[1], "duplicate": True}
            if row[0] != "ready":
                return _error("gmail_send_unconfirmed")
            db.execute("UPDATE send_commands SET status='unknown' WHERE command_id=?", (command_id,))
        # Claim commits before IO. Crash/timeout stays unknown and must never resend.
        result = _bounded_call(lambda: {"ok": True, "id": _text(_call("send_message", raw=raw,
                                    idempotency_key=command_id)["id"], 256)})
        if not result["ok"]:
            result["delivery_status"] = "unknown"
            result["message"] += " " + ERRORS["gmail_send_unconfirmed"]
            return result
        if not result["id"]:
            return _error("gmail_send_unconfirmed")
        with db:
            db.execute("UPDATE send_commands SET status='sent', message_id=? WHERE command_id=?", (result["id"], command_id))
        return result

# Portable contract adapters. UI projections above deliberately remain separate.
def _portable_call(fn, operation):
    try:
        result = fn()
    except gmail.GmailProviderError as exc:
        result = _error(exc.code)
    except PermissionError:
        result = _error('gmail_permission_denied')
    except Exception:
        result = _error('gmail_provider_unavailable')
    if result.get('ok'):
        return result
    code = {
        'gmail_account_not_connected': 'account_not_connected',
        'gmail_reconnect_required': 'account_not_connected',
        'gmail_permission_denied': 'permission_denied',
        'gmail_required_scope_missing': 'permission_denied',
        'google_oauth_not_configured': 'oauth_not_configured',
    }.get(result.get('error'), 'provider_unavailable')
    if operation == 'prepare_send':
        code = 'permission_denied'
    elif operation == 'send_message' and code not in ('account_not_connected', 'permission_denied'):
        code = 'send_unconfirmed'
    elif operation == 'begin_connection' and code == 'account_not_connected':
        code = 'oauth_not_configured'
    elif operation != 'begin_connection' and code == 'oauth_not_configured':
        code = 'provider_unavailable'
    response = {'ok': False, 'error': code, 'message': result['message'][:2048]}
    if operation == 'send_message':
        response['delivery_status'] = 'unknown'
    return response


def _portable_message(raw, body=False):
    row = _message(raw, body)
    return {'id': row['id'], 'threadId': row['thread_id'], 'from': row['from'],
            'date': row['date'], 'subject': row['Message']['subject'],
            'body': row['Message']['body'], 'snippet': _text(raw.get('snippet'), 2048),
            'labelIds': row['labels'], 'isUnread': row['unread'], 'isStarred': row['starred']}


@tool(summary='Portable account readiness.', side_effects='none')
def portable_connection_status():
    def read():
        _authorize()
        raw = _call('connection_status')
        return {'ok': True, 'status': _text(raw.get('status'), 2048),
                'email_address': _text(raw.get('email_address'), 2048)}
    return _portable_call(read, 'connection_status')


@tool(summary='Portable OAuth connection.', side_effects='external_write')
def portable_begin_connection():
    result = _portable_call(begin_connection, 'begin_connection')
    if result['ok']:
        for key, limit in (('authorization_url', 8192), ('expires_at', 2048)):
            if key in result:
                result[key] = _text(result[key], limit)
    return result


@tool(summary='Portable label listing.', side_effects='none')
def portable_list_labels():
    def read():
        _authorize()
        return {'ok': True, 'items': [{'id': _text(x['id'], 256), 'name': _text(x['name'], 2048)}
                for x in _call('list_labels').get('labels', [])[:500]]}
    return _portable_call(read, 'list_labels')


def _portable_flag(value):
    if isinstance(value, str):
        return {'true': True, 'false': False, '': ''}[value.lower()]
    return value


@tool(summary='Portable message search.', side_effects='none')
def portable_list_messages(label='', page_token='', query='', starred='', tab='', unread=''):
    def read():
        _authorize()
        filters = []
        for field, value in (('unread', unread), ('starred', starred)):
            flag = _portable_flag(value)
            if flag != '':
                filters.append(('' if flag else '-') + 'is:' + field)
        if tab:
            filters.append('category:' + tab)
        search = ' '.join((['(' + query + ')'] if query else []) + filters)
        raw = _call('list_message_summaries', query=search,
                    label_ids=[label] if label else [],
                    page_token=page_token, max_results=25)
        token = _text(raw.get('nextPageToken'), 2048)
        return {'ok': True, 'items': [_portable_message(x)
                for x in raw.get('messages', [])[:25]], 'nextPageToken': token,
                'pagination': [{'id': 'next', 'nextPageToken': token, 'summary': 'Next page'}] if token else []}
    return _portable_call(read, 'list_messages')


@tool(summary='Portable message reader.', side_effects='none')
def portable_get_message(id=''):
    def read():
        _authorize()
        if not id:
            return {'ok': True}
        return {'ok': True, 'item': _portable_message(_call('get_message', message_id=id, format='full'), True)}
    return _portable_call(read, 'get_message')


@tool(summary='Portable explicit message change.', side_effects='external_write')
def portable_mutate_message(id, action, label=''):
    def change():
        _authorize(True)
        if action == 'trash':
            raw = _call('trash_message', message_id=id)
        else:
            target, add = {'archive': ('INBOX', False), 'mark_read': ('UNREAD', False),
                'mark_unread': ('UNREAD', True), 'star': ('STARRED', True), 'unstar': ('STARRED', False),
                'label': (label, True), 'remove_label': (label, False)}[action]
            if not target:
                return _error('invalid_input')
            raw = _call('modify_message', message_id=id, add_label_ids=[target] if add else [],
                        remove_label_ids=[] if add else [target])
        return {'ok': True, 'item': _portable_message(raw)}
    return _portable_call(change, 'mutate_message')


@tool(summary='Portable durable send command.', side_effects='local_write')
def portable_prepare_send():
    return _portable_call(prepare_send, 'prepare_send')


@tool(summary='Portable send once.', side_effects='external_write')
def portable_send_message(command_id, values):
    result = _portable_call(lambda: send_message(command_id, **values), 'send_message')
    if result['ok']:
        result['delivery_status'] = 'confirmed'
    return result
