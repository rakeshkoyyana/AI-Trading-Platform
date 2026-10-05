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
    if len(parts) != 3 or parts[0] != PREFIX or parts[1] not in {"approve", "reject"} or not parts[2].isdigit():
        return False, "Unknown button."
    if user_id not in set(approver_ids):
        return False, "You are not allowed to approve trades."
    approve, pid = parts[1] == "approve", int(parts[2])
    who = user_name or str(user_id)
    if control.decide(engine, pid, approve, note=f"via Discord ({who})"):
        return True, (f"✅ Approved by {who} - sending the order." if approve else f"❌ Rejected by {who}.")
    return False, "Too late: this request was already decided or has expired."


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
