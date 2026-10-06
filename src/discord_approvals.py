"""Approve / Reject pending trades from Discord buttons (optional; needs a free Discord bot).

The scheduler posts each "ask" proposal through the bot with two buttons. A click from an allowed user calls
`control.decide` - exactly what the dashboard buttons do - and the scheduler's approval job sends the order.
The bot only does anything when DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID and DISCORD_APPROVER_IDS are all set.
"""
from __future__ import annotations

import asyncio
import threading

from src.execution import control

PREFIX = "aw"


def custom_id(action: str, pending_id: int) -> str:
    return f"{PREFIX}:{action}:{pending_id}"


def handle_click(engine, cid: str, user_id: int, approver_ids, user_name: str = "") -> tuple[bool, str]:
    """Pure decision logic for one button click. Returns (changed, message shown to the clicker/channel)."""
    parts = (cid or "").split(":")
    if (len(parts) != 3 or parts[0] != PREFIX or not parts[2].isdigit()
            or parts[1] not in {"approve", "reject", "testapprove", "testreject"}):
        return False, "Unknown button."
    if user_id not in set(approver_ids):
        return False, "You are not allowed to approve trades."
    who = user_name or str(user_id)
    if parts[1].startswith("test"):  # the self-test message: proves the buttons work, touches nothing
        word = "Approve" if parts[1] == "testapprove" else "Reject"
        return True, f"🧪 Test OK: the {word} button works for {who}. Nothing was traded."
    approve, pid = parts[1] == "approve", int(parts[2])
    if control.decide(engine, pid, approve, note=f"via Discord ({who})"):
        return True, (f"✅ Approved by {who} - sending the order." if approve else f"❌ Rejected by {who}.")
    return False, "Too late: this request was already decided or has expired."


def test_message_payload() -> dict:
    """A fake proposal with the real buttons (REST format). Clicking it never touches orders or the database."""
    return {
        "content": ("🧪 **TEST proposal** - APPROVE? LONG TEST x1 @ ~100.00 | stop 99.00, target 102.00\n"
                    "Click a button to check that Discord approvals work. Nothing will be traded."),
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 3, "label": "Approve", "custom_id": custom_id("testapprove", 0)},
            {"type": 2, "style": 4, "label": "Reject", "custom_id": custom_id("testreject", 0)},
        ]}],
    }


def send_test_message(settings, post=None) -> tuple[bool, str]:
    """Post the test proposal through Discord's REST API (no second gateway connection, so it can run while the
    scheduler is up: the scheduler's bot is the one that answers the click)."""
    if not DiscordApprovals.configured(settings):
        return False, "Not configured: set DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID and DISCORD_APPROVER_IDS in .env."
    import requests

    post = post or requests.post
    r = post(f"https://discord.com/api/v10/channels/{settings.discord_channel_id}/messages",
             headers={"Authorization": f"Bot {settings.discord_bot_token}"}, json=test_message_payload(), timeout=15)
    if 200 <= r.status_code < 300:
        return True, "Test message sent. Open Discord and click Approve / Reject."
    hint = {401: "the bot token is wrong", 403: "the bot can't post there (re-invite it, or give it View Channel + Send Messages)",
            404: "the channel ID is wrong or the bot isn't in that server"}.get(r.status_code, "see the status above")
    return False, f"Discord said {r.status_code}: {hint}."


class DiscordApprovals:
    """Runs a small Discord bot in a background thread inside the scheduler process."""

    def __init__(self, token: str, channel_id: int, approver_ids, engine):
        self.token, self.channel_id, self.approver_ids, self.engine = token, channel_id, tuple(approver_ids), engine
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._ready = threading.Event()

    @staticmethod
    def configured(s) -> bool:
        return bool(s.discord_bot_token and s.discord_channel_id and s.discord_approver_ids)

    def start(self) -> bool:
        try:
            import discord
        except ImportError:
            print("[discord_approvals] discord.py is not installed; run: pip install discord.py")
            return False
        self._discord = discord
        t = threading.Thread(target=self._run, name="discord-approvals", daemon=True)
        t.start()
        return True

    def _run(self) -> None:
        discord = self._discord
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        client = discord.Client(intents=discord.Intents.default())
        self._client = client

        @client.event
        async def on_ready():
            self._ready.set()
            print(f"[discord_approvals] connected as {client.user}")

        @client.event
        async def on_interaction(interaction):
            data = interaction.data or {}
            cid = data.get("custom_id", "")
            if interaction.type != discord.InteractionType.component or not cid.startswith(PREFIX + ":"):
                return
            ok, msg = handle_click(self.engine, cid, interaction.user.id, self.approver_ids, str(interaction.user))
            if ok:
                body = f"{interaction.message.content}\n\n{msg}"
                await interaction.response.edit_message(content=body[:1900], view=None)
            else:
                await interaction.response.send_message(msg, ephemeral=True)

        try:
            self._loop.run_until_complete(client.start(self.token))
        except Exception as exc:  # noqa: BLE001
            print(f"[discord_approvals] stopped: {exc}")

    def post_proposal(self, pending_id: int, text: str, timeout: float = 15.0) -> bool:
        """Post a proposal with Approve / Reject buttons. False means it was not posted (caller falls back)."""
        if not self._loop or not self._ready.wait(timeout=5):
            return False
        discord = self._discord

        async def _send():
            ch = self._client.get_channel(self.channel_id) or await self._client.fetch_channel(self.channel_id)
            view = discord.ui.View(timeout=None)
            view.add_item(discord.ui.Button(label="Approve", style=discord.ButtonStyle.success,
                                            custom_id=custom_id("approve", pending_id)))
            view.add_item(discord.ui.Button(label="Reject", style=discord.ButtonStyle.danger,
                                            custom_id=custom_id("reject", pending_id)))
            await ch.send(text[:1900], view=view)

        try:
            asyncio.run_coroutine_threadsafe(_send(), self._loop).result(timeout=timeout)
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[discord_approvals] could not post proposal: {exc}")
            return False

    def stop(self) -> None:
        if self._loop and self._client:
            try:
                asyncio.run_coroutine_threadsafe(self._client.close(), self._loop).result(timeout=5)
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    import sys

    from src.config import get_settings

    if "--test" in sys.argv:
        ok, msg = send_test_message(get_settings())
        print(msg)
        sys.exit(0 if ok else 1)
    print("usage: python -m src.discord_approvals --test")
