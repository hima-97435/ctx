"""Synthetic, real-shaped official exports (ChatGPT, Claude.ai, Gemini Takeout) built as zips."""

import json
import zipfile
from pathlib import Path

FAKE_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"  # fake, shape-only
SECRET_LINE = "api_key=abcdef123456xyz"
CANARY = "CANARY-ACCOUNT-EMAIL-9f3a"  # lives only in files that must never be read


def make_zip(path: Path, members: dict[str, object]) -> Path:
    """members: name -> str | bytes | JSON-serialisable object."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in members.items():
            if isinstance(content, bytes):
                data = content
            elif isinstance(content, str):
                data = content.encode()
            else:
                data = json.dumps(content).encode()
            zf.writestr(name, data)
    return path


# ---- ChatGPT -----------------------------------------------------------------------
def gpt_msg(mid, role, ctype, parts_or_fields, ts, parent, children=(), **msg_extra):
    content = {"content_type": ctype}
    if isinstance(parts_or_fields, dict):
        content.update(parts_or_fields)
    else:
        content["parts"] = parts_or_fields
    message = {
        "id": mid,
        "author": {"role": role, "name": None, "metadata": {}},
        "create_time": ts,
        "update_time": None,
        "content": content,
        "status": "finished_successfully",
        "metadata": msg_extra.pop("metadata", {}),
        "recipient": msg_extra.pop("recipient", "all"),
    }
    message.update(msg_extra)
    return {"id": mid, "message": message, "parent": parent, "children": list(children)}


def gpt_conversation(cid="conv-1", title="Plan the migration", extra_tail=False):
    """root -> sys(hidden) -> u1 -> a1 (-> a1-alt regenerated) -> u2 -> code -> out -> a2."""
    t = 1_760_000_000.0
    m = {
        "root": {"id": "root", "message": None, "parent": None, "children": ["sys"]},
        "sys": gpt_msg(
            "sys",
            "system",
            "text",
            [""],
            t,
            "root",
            ["u1"],
            metadata={"is_visually_hidden_from_conversation": True},
        ),
        "u1": gpt_msg(
            "u1", "user", "text", ["How do I migrate the db?"], t + 1, "sys", ["a1", "a1-alt"]
        ),
        "a1": gpt_msg(
            "a1",
            "assistant",
            "text",
            ["Use alembic. Note " + SECRET_LINE],
            t + 2,
            "u1",
            ["u2"],
            metadata={"model_slug": "gpt-4o"},
        ),
        "a1-alt": gpt_msg("a1-alt", "assistant", "text", ["ALTERNATE-BRANCH-TEXT"], t + 2, "u1"),
        "u2": gpt_msg(
            "u2",
            "user",
            "multimodal_text",
            [
                {
                    "content_type": "image_asset_pointer",
                    "asset_pointer": "file-service://file-x",
                    "width": 640,
                    "height": 480,
                },
                "what is in this screenshot?",
            ],
            t + 3,
            "a1",
            ["c1"],
        ),
        "c1": gpt_msg(
            "c1",
            "assistant",
            "code",
            {"language": "python", "text": "print('hi')"},
            t + 4,
            "u2",
            ["o1"],
            recipient="python",
        ),
        "o1": gpt_msg(
            "o1",
            "tool",
            "execution_output",
            {"text": "hi"},
            t + 5,
            "c1",
            ["b1"],
        ),
        "b1": gpt_msg(
            "b1",
            "tool",
            "tether_browsing_display",
            {"result": "Search result page", "summary": None},
            t + 6,
            "o1",
            ["q1"],
        ),
        "q1": gpt_msg(
            "q1",
            "tool",
            "tether_quote",
            {
                "url": "https://example.com",
                "domain": "example.com",
                "text": "quoted",
                "title": "Ex",
            },
            t + 7,
            "b1",
            ["w1"],
        ),
        "w1": gpt_msg("w1", "tool", "weird_new_type", {"foo": "bar"}, t + 8, "q1", ["a2"]),
        "a2": gpt_msg(
            "a2",
            "assistant",
            "text",
            ["Done. token " + FAKE_TOKEN],
            t + 9,
            "w1",
            ["u3"] if extra_tail else [],
            metadata={"model_slug": "gpt-4o"},
        ),
    }
    current = "a2"
    if extra_tail:
        m["u3"] = gpt_msg("u3", "user", "text", ["thanks, one more thing"], t + 10, "a2")
        current = "u3"
    return {
        "id": cid,
        "conversation_id": cid,
        "title": title,
        "create_time": t,
        "update_time": t + 20,
        "mapping": m,
        "current_node": current,
        "default_model_slug": "gpt-4o",
    }


def chatgpt_zip(path: Path, convs=None, sharded=False, extra=None) -> Path:
    convs = convs if convs is not None else [gpt_conversation()]
    members: dict[str, object] = {
        "user.json": {"id": "user-1", "email": CANARY + "@example.com"},
        "message_feedback.json": [{"note": CANARY}],
        "chat.html": "<html>" + CANARY + "</html>",
    }
    if sharded:
        for i, conv in enumerate(convs):
            members[f"conversations-{i:03d}.json"] = [conv]
    else:
        members["conversations.json"] = convs
    members.update(extra or {})
    return make_zip(path, members)


# ---- Claude.ai ---------------------------------------------------------------------
def claude_conversation(uuid="cl-1", extra_message=False):
    msgs = [
        {
            "uuid": "m1",
            "sender": "human",
            "created_at": "2026-10-01T10:00:00.000000Z",
            "text": "Summarize attached",
            "content": [{"type": "text", "text": "Summarize attached"}],
            "attachments": [
                {
                    "file_name": "notes.txt",
                    "file_size": 10,
                    "file_type": "txt",
                    "extracted_content": "doc body " + SECRET_LINE,
                }
            ],
            "files": [{"file_name": "pic.png"}],
        },
        {
            "uuid": "m2",
            "sender": "assistant",
            "created_at": "2026-10-01T10:00:05.000000Z",
            "text": "Here is the summary",
            "content": [
                {"type": "thinking", "thinking": "think about it"},
                {"type": "text", "text": "Here is the summary " + FAKE_TOKEN},
                {"type": "tool_use", "name": "web_search", "input": {"query": "x"}, "id": "t1"},
                {"type": "tool_result", "content": [{"type": "text", "text": "result text"}]},
                {"type": "flux_capacitor", "value": 1},
            ],
            "attachments": [],
            "files": [],
        },
        {  # older shape: no content blocks, only text
            "uuid": "m3",
            "sender": "human",
            "created_at": "2026-10-01T10:01:00.000000Z",
            "text": "plain text only",
            "content": [],
            "attachments": [],
            "files": [],
        },
    ]
    if extra_message:
        msgs.append(
            {
                "uuid": "m4",
                "sender": "assistant",
                "created_at": "2026-10-01T10:02:00.000000Z",
                "text": "later reply",
                "content": [{"type": "text", "text": "later reply"}],
                "attachments": [],
                "files": [],
            }
        )
    return {
        "uuid": uuid,
        "name": "Doc summary",
        "created_at": "2026-10-01T10:00:00.000000Z",
        "updated_at": "2026-10-01T10:02:00.000000Z",
        "project_uuid": "p-1",
        "account": {"uuid": "acct-1"},
        "chat_messages": msgs,
    }


def claude_zip(path: Path, convs=None, projects=True, extra=None) -> Path:
    members: dict[str, object] = {
        "users.json": [{"uuid": "acct-1", "email_address": CANARY + "@example.com"}],
        "conversations.json": convs if convs is not None else [claude_conversation()],
    }
    if projects:
        members["projects.json"] = [{"uuid": "p-1", "name": "Research", "docs": [{"content": "x"}]}]
    members.update(extra or {})
    return make_zip(path, members)


# ---- Gemini Takeout -----------------------------------------------------------------
GEMINI_JSON_PATH = "Takeout/My Activity/Gemini Apps/MyActivity.json"
GEMINI_HTML_PATH = "Takeout/My Activity/Gemini Apps/MyActivity.html"


def gemini_entry(time, prompt, response=None, header="Gemini Apps"):
    entry = {
        "header": header,
        "title": f"Prompted {prompt}",
        "time": time,
        "products": [header],
        "activityControls": ["Gemini Apps Activity"],
    }
    if response is not None:
        entry["safeHtmlItem"] = [{"html": response}]
    return entry


def gemini_entries():
    # Takeout lists newest first
    return [
        gemini_entry("2026-10-01T12:00:00.000Z", "later question", "<p>later answer</p>"),
        gemini_entry(
            "2026-10-01T10:20:00.000Z",
            "second " + SECRET_LINE,
            "<p>Answer <b>two</b></p><ul><li>a</li><li>b</li></ul>",
        ),
        gemini_entry("2026-10-01T10:00:00.000Z", "first question", "<p>Answer one</p>"),
        {
            "header": "Gemini Apps",
            "title": "Used Gemini Apps",
            "time": "2026-10-01T09:00:00.000Z",
            "products": ["Gemini Apps"],
        },
        gemini_entry("2026-10-01T10:05:00.000Z", "search thing", None, header="Search"),
    ]


def gemini_html(rows):
    """rows: (prompt, 'Oct 1, 2026, 10:00:00 AM UTC', response_html)."""
    cells = []
    for prompt, when, resp in rows:
        cells.append(
            '<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">'
            '<div class="mdl-grid"><div class="header-cell mdl-cell mdl-cell--12-col">'
            '<p class="mdl-typography--title">Gemini Apps<br></p></div>'
            '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
            f"Prompted&nbsp;{prompt}<br>{when}<br>{resp}</div>"
            '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1 '
            'mdl-typography--text-right"></div>'
            '<div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption">'
            "<b>Products:</b><br>&emsp;Gemini Apps<br></div></div></div>"
        )
    return "<html><body>" + "".join(cells) + "</body></html>"


def gemini_zip(path: Path, entries=None, html=None, extra=None) -> Path:
    members: dict[str, object] = {
        "Takeout/My Activity/Search/MyActivity.json": [
            {"header": "Search", "title": "Searched for " + CANARY, "time": "2026-10-01T10:00:00Z"}
        ],
        "Takeout/Google Account/profile.json": {"email": CANARY},
    }
    if html is not None:
        members[GEMINI_HTML_PATH] = html
    else:
        members[GEMINI_JSON_PATH] = entries if entries is not None else gemini_entries()
    members.update(extra or {})
    return make_zip(path, members)
