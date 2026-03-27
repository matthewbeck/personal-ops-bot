import os
import json
import yaml
import httpx
import hmac
import hashlib
import time
from pathlib import Path
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()

SLACK_BOT_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
SLACK_SIGNING_SECRET = os.environ["SLACK_SIGNING_SECRET"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GMAIL_MCP_URL     = os.getenv("GMAIL_MCP_URL", "https://gmail.mcp.claude.com/mcp")
GCAL_MCP_URL      = os.getenv("GCAL_MCP_URL", "https://gcal.mcp.claude.com/mcp")

AGENTS_DIR = Path(__file__).parent / "agents"

def load_agents() -> dict:
    agents = {}
    for f in AGENTS_DIR.glob("*.yaml"):
        config = yaml.safe_load(f.read_text())
        agents[config["slack_channel"]] = config
    return agents

AGENTS = load_agents()

def get_agent_for_channel(channel_name: str) -> dict | None:
    return AGENTS.get(f"#{channel_name}")

def verify_slack_signature(request_body: bytes, timestamp: str, signature: str) -> bool:
    if abs(time.time() - int(timestamp)) > 60 * 5:
        return False
    sig_basestring = f"v0:{timestamp}:{request_body.decode()}"
    my_sig = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(my_sig, signature)

async def slack_post(endpoint: str, payload: dict):
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"https://slack.com/api/{endpoint}",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            json=payload
        )
        return r.json()

async def slack_get(endpoint: str, params: dict):
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"https://slack.com/api/{endpoint}",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            params=params
        )
        return r.json()

async def post_message(channel: str, text: str, thread_ts: str = None):
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return await slack_post("chat.postMessage", payload)

PENDING_APPROVALS: dict = {}

async def call_claude(agent: dict, user_message: str, conversation_history: list = None) -> str:
    tools = []
    if "web_search" in agent.get("tools", []):
        tools.append({"type": "web_search_20250305", "name": "web_search"})

    messages = conversation_history or []
    messages.append({"role": "user", "content": user_message})

    body = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 1000,
        "system": agent["system_prompt"],
        "messages": messages,
    }
    if tools:
        body["tools"] = tools

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=body
        )
        data = r.json()
        print(f"🤖 Claude response: {json.dumps(data)[:300]}")

    text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    return "\n".join(text_blocks) or "No response generated."

def format_approval_message(action_type: str, data: dict) -> str:
    if action_type == "email":
        return (
            f"📧 *Email ready for approval*\n\n"
            f"*To:* {data['to']}\n"
            f"*Subject:* {data['subject']}\n\n"
            f"```{data['body']}```\n\n"
            f"React ✅ to send • ❌ to discard"
        )
    elif action_type == "calendar":
        return (
            f"📅 *Calendar event ready for approval*\n\n"
            f"*Title:* {data['title']}\n"
            f"*Time:* {data['time']}\n"
            f"*Attendees:* {data.get('attendees', 'None')}\n\n"
            f"React ✅ to book • ❌ to discard"
        )
    return str(data)

async def handle_message(event: dict):
    try:
        channel_id  = event.get("channel")
        text        = event.get("text", "").strip()
        thread_ts   = event.get("thread_ts")
        bot_id      = event.get("bot_id")

        if bot_id:
            return

        bot_user_id = os.getenv("SLACK_BOT_USER_ID", "")
        if bot_user_id and f"<@{bot_user_id}>" not in text:
            return

        agent = event.get("_agent")
        if not agent:
            return

        print(f"📞 Calling Claude API...")
        response = await call_claude(agent, text)
        print(f"📞 Claude responded: {response[:100]}")
        await post_message(channel_id, response, thread_ts)

    except Exception as e:
        print(f"❌ handle_message error: {e}")
        import traceback
        traceback.print_exc()

async def handle_reaction(event: dict):
    reaction   = event.get("reaction")
    item       = event.get("item", {})
    message_ts = item.get("ts")

    if message_ts not in PENDING_APPROVALS:
        return

    approval = PENDING_APPROVALS.pop(message_ts)
    channel  = approval["channel"]
    agent    = approval["agent"]

    if reaction == "white_check_mark":
        if approval["type"] == "email":
            result = await call_claude(agent, f"SYSTEM: User approved. Send this email now. Data: {json.dumps(approval['data'])}")
            await post_message(channel, f"✅ Email sent.\n{result}", message_ts)
        elif approval["type"] == "calendar":
            result = await call_claude(agent, f"SYSTEM: User approved. Create this calendar event now. Data: {json.dumps(approval['data'])}")
            await post_message(channel, f"✅ Calendar event created.\n{result}", message_ts)
    elif reaction == "x":
        await post_message(channel, "❌ Action discarded.", message_ts)

@app.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks):
    body_bytes = await request.body()
    timestamp  = request.headers.get("X-Slack-Request-Timestamp", "")
    signature  = request.headers.get("X-Slack-Signature", "")

    if not verify_slack_signature(body_bytes, timestamp, signature):
        print("❌ Signature verification failed")
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(body_bytes)
    print(f"✅ Event received: {payload.get('type')} / {payload.get('event', {}).get('type')}")

    if payload.get("type") == "url_verification":
        return JSONResponse({"challenge": payload["challenge"]})

    event = payload.get("event", {})
    event_type = event.get("type")

    if event_type == "message":
        channel_id = event.get("channel")
        print(f"📨 Message in channel: {channel_id}")

        channel_info = await slack_get("conversations.info", {"channel": channel_id})
        print(f"🔍 conversations.info response: {channel_info}")
        channel_name = channel_info.get("channel", {}).get("name", "")
        print(f"📨 Channel name: {channel_name}")
        print(f"🔍 Looking up agent for: '#{channel_name}' — available agents: {list(AGENTS.keys())}")

        agent = get_agent_for_channel(channel_name)
        print(f"🤖 Agent found: {agent is not None}")

        if agent:
            event["_agent"] = agent
            background_tasks.add_task(handle_message, event)

    elif event_type == "reaction_added":
        background_tasks.add_task(handle_reaction, event)

    return JSONResponse({"ok": True})

@app.get("/health")
async def health():
    return {"status": "ok", "agents": list(AGENTS.keys())}
