import os
import hmac
import hashlib
import json
import threading
import urllib.request
import urllib.error
from flask import Flask, request, jsonify
import anthropic

app = Flask(__name__)

SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
NOTION_PAPER_DB_ID = os.environ.get("NOTION_PAPER_DB_ID")
SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", "You are a helpful assistant.")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

processed_events = set()
lock = threading.Lock()

def verify_slack_signature(req):
    timestamp = req.headers.get("X-Slack-Request-Timestamp", "")
    signature = req.headers.get("X-Slack-Signature", "")
    body = req.get_data(as_text=True)
    sig_basestring = f"v0:{timestamp}:{body}"
    computed = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed, signature)

def send_slack_message(channel, text):
    payload = json.dumps({"channel": channel, "text": text}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json"
        }
    )
    with urllib.request.urlopen(req) as response:
        result = json.loads(response.read().decode())
        if not result.get("ok"):
            print(f"Slack error: {result.get('error')}")

def add_to_notion_paper_db(title, summary, score=5):
    if not NOTION_API_KEY or not NOTION_PAPER_DB_ID:
        return
    payload = json.dumps({
        "parent": {"database_id": NOTION_PAPER_DB_ID},
        "properties": {
            "タイトル": {
                "title": [{"text": {"content": title}}]
            },
            "3行要約": {
                "rich_text": [{"text": {"content": summary}}]
            },
            "重要度スコア": {
                "number": score
            },
            "ステータス": {
                "select": {"name": "要約済"}
            }
        }
    }).encode()
    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=payload,
        headers={
            "Authorization": f"Bearer {NOTION_API_KEY}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28"
        }
    )
    try:
        with urllib.request.urlopen(req) as response:
            print(f"Notion page created successfully")
    except Exception as e:
        print(f"Notion error: {e}")

def handle_event(event, event_id):
    try:
        user_message = event.get("text", "")
        channel = event.get("channel")

        notion_prompt = ""
        if NOTION_API_KEY and NOTION_PAPER_DB_ID:
            notion_prompt = """
論文や知識をNotionに登録する場合は、返答の最後に以下の形式で記載してください：
NOTION_REGISTER:
タイトル: [論文タイトル]
要約: [3行要約]
スコア: [1-10の数値]
END_NOTION
"""

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=SYSTEM_PROMPT + notion_prompt,
            messages=[{"role": "user", "content": user_message}]
        )

        reply = response.content[0].text

        if "NOTION_REGISTER:" in reply and "END_NOTION" in reply:
            notion_part = reply.split("NOTION_REGISTER:")[1].split("END_NOTION")[0].strip()
            clean_reply = reply.split("NOTION_REGISTER:")[0].strip()

            lines = notion_part.split("\n")
            title = ""
            summary = ""
            score = 5
            for line in lines:
                if line.startswith("タイトル:"):
                    title = line.replace("タイトル:", "").strip()
                elif line.startswith("要約:"):
                    summary = line.replace("要約:", "").strip()
                elif line.startswith("スコア:"):
                    try:
                        score = int(line.replace("スコア:", "").strip())
                    except:
                        score = 5

            if title:
                add_to_notion_paper_db(title, summary, score)
                clean_reply += "\n\n✅ Notionの論文・知識DBに登録しました"

            send_slack_message(channel, clean_reply)
        else:
            send_slack_message(channel, reply)

    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        send_slack_message(event.get("channel"), f"エラーが発生しました: {e}")

@app.route("/slack/events", methods=["POST"])
def slack_events():
    data = request.json

    if data.get("type") == "url_verification":
        return jsonify({"challenge": data["challenge"]})

    if not verify_slack_signature(request):
        return jsonify({"error": "Invalid signature"}), 403

    event = data.get("event", {})
    event_id = data.get("event_id", "")

    with lock:
        if event_id in processed_events:
            return jsonify({"status": "duplicate"}), 200
        processed_events.add(event_id)

    if event.get("type") == "app_mention" and not event.get("bot_id"):
        thread = threading.Thread(target=handle_event, args=(event, event_id))
        thread.start()

    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
