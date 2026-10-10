import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------- CONFIG ----
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))


def ids(name: str) -> list[int]:
    return [int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x]


def one_id(name: str) -> int:
    return int(os.getenv(name, "0") or 0)


STAFF_ROLE_IDS = ids("STAFF_ROLE_IDS")          # roles allowed to use staff commands
LOG_CHANNEL_ID = one_id("LOG_CHANNEL_ID")        # fallback log channel
TICKET_CATEGORY_ID = one_id("TICKET_CATEGORY_ID")
WARRANT_CHANNEL_ID = one_id("WARRANT_CHANNEL_ID")
ARREST_CHANNEL_ID = one_id("ARREST_CHANNEL_ID")
PROMOTION_CHANNEL_ID = one_id("PROMOTION_CHANNEL_ID")
INFRACTION_CHANNEL_ID = one_id("INFRACTION_CHANNEL_ID")

# -------------------------------------------------------------- DATABASE ----
DB_PATH = os.getenv("DB_PATH", "bot.db")  # on Railway, set to /data/bot.db
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER, user_id INTEGER, subject TEXT,
    status TEXT DEFAULT 'open', created_at TEXT
);
CREATE TABLE IF NOT EXISTS warrants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    suspect TEXT, reason TEXT, issued_by INTEGER,
    status TEXT DEFAULT 'active', created_at TEXT
);
CREATE TABLE IF NOT EXISTS arrests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    suspect TEXT, charges TEXT, notes TEXT, officer_id INTEGER,
    warrant_id INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS promotions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, old_rank TEXT, new_rank TEXT, reason TEXT,
    by_id INTEGER, created_at TEXT
);
CREATE TABLE IF NOT EXISTS infractions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, type TEXT, reason TEXT, by_id INTEGER, created_at TEXT
);
"""
)
db.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------- HELPERS ----
def embed(title: str, color: discord.Color, **fields) -> discord.Embed:
    e = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    for name, value in fields.items():
        e.add_field(name=name.replace("_", " ").title(), value=str(value) or "N/A", inline=False)
    return e


async def post(guild: discord.Guild, channel_id: int, e: discord.Embed):
    channel = guild.get_channel(channel_id or LOG_CHANNEL_ID)
    if channel:
        await channel.send(embed=e)


def is_staff(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or any(
        r.id in STAFF_ROLE_IDS for r in member.roles
    )


def staff_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_staff(interaction.user):
            return True
        raise app_commands.CheckFailure("You don't have permission to use this command.")

    return app_commands.check(predicate)


# ---------------------------------------------------------------- TICKETS ----
class TicketModal(discord.ui.Modal, title="Open a Ticket"):
    subject = discord.ui.TextInput(label="Subject", max_length=100)
    details = discord.ui.TextInput(
        label="Details", style=discord.TextStyle.paragraph, max_length=1000
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild, user = interaction.guild, interaction.user

        row = db.execute(
            "SELECT id, channel_id FROM tickets WHERE user_id=? AND status='open'", (user.id,)
        ).fetchone()
        if row:
            if guild.get_channel(row["channel_id"]):
                return await interaction.response.send_message(
                    f"You already have an open ticket: <#{row['channel_id']}>", ephemeral=True
                )
            db.execute("UPDATE tickets SET status='closed' WHERE id=?", (row["id"],))
            db.commit()

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, attach_files=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True
            ),
        }
        for rid in STAFF_ROLE_IDS:
            role = guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        category = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
        channel = await guild.create_text_channel(
            f"ticket-{user.name}", category=category, overwrites=overwrites
        )
        db.execute(
            "INSERT INTO tickets (channel_id, user_id, subject, created_at) VALUES (?,?,?,?)",
            (channel.id, user.id, self.subject.value, now()),
        )
        db.commit()

        e = embed(
            "🎫 New Ticket",
            discord.Color.blurple(),
            opened_by=user.mention,
            subject=self.subject.value,
            details=self.details.value,
        )
        await channel.send(content=user.mention, embed=e, view=CloseView())
        await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)
        await post(guild, LOG_CHANNEL_ID, embed("Ticket Opened", discord.Color.green(),
                                                user=user.mention, channel=channel.mention))


class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", emoji="🎫", style=discord.ButtonStyle.primary,
                       custom_id="ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketModal())


class CloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", emoji="🔒", style=discord.ButtonStyle.danger,
                       custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = db.execute(
            "SELECT * FROM tickets WHERE channel_id=? AND status='open'", (interaction.channel.id,)
        ).fetchone()
        if not row:
            return await interaction.response.send_message("Ticket not found.", ephemeral=True)
        if not (is_staff(interaction.user) or interaction.user.id == row["user_id"]):
            return await interaction.response.send_message("Only staff or the ticket owner can close this.", ephemeral=True)

        db.execute("UPDATE tickets SET status='closed' WHERE id=?", (row["id"],))
        db.commit()
        await interaction.response.send_message("🔒 Closing ticket in 5 seconds...")
        await post(interaction.guild, LOG_CHANNEL_ID, embed(
            "Ticket Closed", discord.Color.red(),
            ticket=f"#{row['id']} - {row['subject']}", closed_by=interaction.user.mention))
        import asyncio
        await asyncio.sleep(5)
        await interaction.channel.delete()


# -------------------------------------------------------------------- BOT ----
class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(TicketPanel())
        self.add_view(CloseView())
        for group in (warrant, arrest, infraction):
            self.tree.add_command(group)
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = Bot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        msg = str(error) or "You can't use this command."
    else:
        print(f"Command error: {error!r}")
        msg = "Something went wrong running that command."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


# ----------------------------------------------------------- TICKET PANEL ----
@bot.tree.command(name="ticketpanel", description="Post the ticket panel in this channel")
@staff_only()
async def ticketpanel(interaction: discord.Interaction):
    e = discord.Embed(
        title="🎫 Support Tickets",
        description="Click the button below to open a private ticket with staff.",
        color=discord.Color.blurple(),
    )
    await interaction.channel.send(embed=e, view=TicketPanel())
    await interaction.response.send_message("Panel posted.", ephemeral=True)


# --------------------------------------------------------------- WARRANTS ----
warrant = app_commands.Group(name="warrant", description="Warrant system")


@warrant.command(name="issue", description="Issue a warrant")
@staff_only()
async def warrant_issue(interaction: discord.Interaction, suspect: str, reason: str):
    cur = db.execute(
        "INSERT INTO warrants (suspect, reason, issued_by, created_at) VALUES (?,?,?,?)",
        (suspect, reason, interaction.user.id, now()),
    )
    db.commit()
    e = embed(f"🚨 Warrant #{cur.lastrowid} Issued", discord.Color.red(),
              suspect=suspect, reason=reason, issued_by=interaction.user.mention, status="ACTIVE")
    await post(interaction.guild, WARRANT_CHANNEL_ID, e)
    await interaction.response.send_message(f"Warrant #{cur.lastrowid} issued.", ephemeral=True)


@warrant.command(name="list", description="List active warrants")
@staff_only()
async def warrant_list(interaction: discord.Interaction):
    rows = db.execute("SELECT * FROM warrants WHERE status='active' ORDER BY id DESC LIMIT 25").fetchall()
    if not rows:
        return await interaction.response.send_message("No active warrants.", ephemeral=True)
    desc = "\n".join(f"**#{r['id']}** - {r['suspect']}: {r['reason'][:80]}" for r in rows)
    await interaction.response.send_message(
        embed=discord.Embed(title="Active Warrants", description=desc, color=discord.Color.red()),
        ephemeral=True)


@warrant.command(name="view", description="View a warrant")
@staff_only()
async def warrant_view(interaction: discord.Interaction, warrant_id: int):
    r = db.execute("SELECT * FROM warrants WHERE id=?", (warrant_id,)).fetchone()
    if not r:
        return await interaction.response.send_message("Warrant not found.", ephemeral=True)
    e = embed(f"Warrant #{r['id']}", discord.Color.red(), suspect=r["suspect"], reason=r["reason"],
              issued_by=f"<@{r['issued_by']}>", status=r["status"].upper(), created=r["created_at"])
    await interaction.response.send_message(embed=e, ephemeral=True)


async def set_warrant_status(interaction: discord.Interaction, warrant_id: int, status: str):
    r = db.execute("SELECT * FROM warrants WHERE id=?", (warrant_id,)).fetchone()
    if not r:
        return await interaction.response.send_message("Warrant not found.", ephemeral=True)
    db.execute("UPDATE warrants SET status=? WHERE id=?", (status, warrant_id))
    db.commit()
    e = embed(f"Warrant #{warrant_id} {status.title()}", discord.Color.orange(),
              suspect=r["suspect"], updated_by=interaction.user.mention)
    await post(interaction.guild, WARRANT_CHANNEL_ID, e)
    await interaction.response.send_message(f"Warrant #{warrant_id} marked {status}.", ephemeral=True)


@warrant.command(name="serve", description="Mark a warrant as served")
@staff_only()
async def warrant_serve(interaction: discord.Interaction, warrant_id: int):
    await set_warrant_status(interaction, warrant_id, "served")


@warrant.command(name="revoke", description="Revoke a warrant")
@staff_only()
async def warrant_revoke(interaction: discord.Interaction, warrant_id: int):
    await set_warrant_status(interaction, warrant_id, "revoked")


# ---------------------------------------------------------------- ARRESTS ----
arrest = app_commands.Group(name="arrest", description="Arrest system")


@arrest.command(name="log", description="Log an arrest")
@staff_only()
async def arrest_log(interaction: discord.Interaction, suspect: str, charges: str,
                     notes: Optional[str] = None, warrant_id: Optional[int] = None):
    cur = db.execute(
        "INSERT INTO arrests (suspect, charges, notes, officer_id, warrant_id, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (suspect, charges, notes or "None", interaction.user.id, warrant_id, now()),
    )
    if warrant_id:
        db.execute("UPDATE warrants SET status='served' WHERE id=?", (warrant_id,))
    db.commit()
    e = embed(f"🚔 Arrest #{cur.lastrowid}", discord.Color.dark_blue(), suspect=suspect,
              charges=charges, notes=notes or "None", arresting_officer=interaction.user.mention,
              warrant=f"#{warrant_id} (served)" if warrant_id else "None")
    await post(interaction.guild, ARREST_CHANNEL_ID, e)
    await interaction.response.send_message(f"Arrest #{cur.lastrowid} logged.", ephemeral=True)


@arrest.command(name="history", description="View a suspect's arrest history")
@staff_only()
async def arrest_history(interaction: discord.Interaction, suspect: str):
    rows = db.execute("SELECT * FROM arrests WHERE suspect LIKE ? ORDER BY id DESC LIMIT 15",
                      (suspect,)).fetchall()
    if not rows:
        return await interaction.response.send_message("No arrests on record.", ephemeral=True)
    desc = "\n".join(f"**#{r['id']}** ({r['created_at'][:10]}): {r['charges'][:100]}" for r in rows)
    await interaction.response.send_message(
        embed=discord.Embed(title=f"Arrest History - {suspect}", description=desc,
                            color=discord.Color.dark_blue()), ephemeral=True)


# ------------------------------------------------------------- PROMOTIONS ----
@bot.tree.command(name="promote", description="Promote a member to a new rank")
@staff_only()
async def promote(interaction: discord.Interaction, member: discord.Member, new_rank: discord.Role,
                  reason: str, old_rank: Optional[discord.Role] = None):
    try:
        if old_rank and old_rank in member.roles:
            await member.remove_roles(old_rank, reason=f"Promotion by {interaction.user}")
        await member.add_roles(new_rank, reason=f"Promotion by {interaction.user}")
    except discord.Forbidden:
        return await interaction.response.send_message(
            "I can't manage that role. Move my bot role above it in Server Settings.", ephemeral=True)

    db.execute(
        "INSERT INTO promotions (user_id, old_rank, new_rank, reason, by_id, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (member.id, old_rank.name if old_rank else "N/A", new_rank.name, reason,
         interaction.user.id, now()))
    db.commit()
    e = embed("📈 Promotion", discord.Color.gold(), member=member.mention,
              old_rank=old_rank.mention if old_rank else "N/A", new_rank=new_rank.mention,
              reason=reason, promoted_by=interaction.user.mention)
    await post(interaction.guild, PROMOTION_CHANNEL_ID, e)
    await interaction.response.send_message(f"Promoted {member.mention} to {new_rank.mention}.", ephemeral=True)


# ------------------------------------------------------------ INFRACTIONS ----
infraction = app_commands.Group(name="infraction", description="Infraction system")
INFRACTION_TYPES = ["Warning", "Strike", "Suspension", "Demotion", "Termination"]


@infraction.command(name="issue", description="Issue an infraction")
@staff_only()
@app_commands.choices(type=[app_commands.Choice(name=t, value=t) for t in INFRACTION_TYPES])
async def infraction_issue(interaction: discord.Interaction, member: discord.Member,
                           type: app_commands.Choice[str], reason: str):
    cur = db.execute(
        "INSERT INTO infractions (user_id, type, reason, by_id, created_at) VALUES (?,?,?,?,?)",
        (member.id, type.value, reason, interaction.user.id, now()))
    db.commit()
    e = embed(f"⚠️ Infraction #{cur.lastrowid}", discord.Color.orange(), member=member.mention,
              type=type.value, reason=reason, issued_by=interaction.user.mention)
    await post(interaction.guild, INFRACTION_CHANNEL_ID, e)
    try:
        await member.send(embed=e)
    except discord.Forbidden:
        pass  # DMs closed
    await interaction.response.send_message(f"Infraction #{cur.lastrowid} issued.", ephemeral=True)


@infraction.command(name="list", description="List a member's infractions")
@staff_only()
async def infraction_list(interaction: discord.Interaction, member: discord.Member):
    rows = db.execute("SELECT * FROM infractions WHERE user_id=? ORDER BY id DESC LIMIT 20",
                      (member.id,)).fetchall()
    if not rows:
        return await interaction.response.send_message("No infractions on record.", ephemeral=True)
    desc = "\n".join(f"**#{r['id']}** [{r['type']}] {r['reason'][:80]} ({r['created_at'][:10]})" for r in rows)
    await interaction.response.send_message(
        embed=discord.Embed(title=f"Infractions - {member.display_name}", description=desc,
                            color=discord.Color.orange()), ephemeral=True)


@infraction.command(name="remove", description="Remove an infraction")
@staff_only()
async def infraction_remove(interaction: discord.Interaction, infraction_id: int):
    cur = db.execute("DELETE FROM infractions WHERE id=?", (infraction_id,))
    db.commit()
    msg = f"Infraction #{infraction_id} removed." if cur.rowcount else "Infraction not found."
    await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file.")
    bot.run(TOKEN)
