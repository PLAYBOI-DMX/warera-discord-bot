
import os
import aiosqlite
import discord
import warera

from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
DB_PATH = "warera_bot.db"

UNVERIFIED_ROLE = "Unverified"
VERIFIED_ROLE = "Verified"  # Legacy role; new linking uses the selected citizenship role.
ADMIN_ROLE = "Admin"
INDIAN_CITIZEN_ROLE = "Indian Citizen"
NRI_ROLE = "NRI"
VERIFICATION_TEAM_ROLE = "Verification Team"
VERIFICATION_CATEGORY = "VERIFICATION"
TICKET_PANEL_CHANNEL = "open-a-ticket"
VERIFICATION_LOG_CHANNEL = "verification-logs"


class TicketPanelView(discord.ui.View):
    """Persistent ticket panel button; custom_id must remain stable."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Open Verification Ticket",
        style=discord.ButtonStyle.primary,
        emoji="🎫",
        custom_id="warera:open_verification_ticket:v1"
    )
    async def open_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await create_verification_ticket(interaction)


class WarEraBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True

        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS members (
                    discord_id INTEGER PRIMARY KEY,
                    verified INTEGER NOT NULL DEFAULT 0,
                    warera_username TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tickets (
                    discord_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ticket_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    discord_id INTEGER NOT NULL,
                    thread_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    closed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    closed_by INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS verification_recommendations (
                    discord_id INTEGER PRIMARY KEY,
                    recommended_by INTEGER NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Safe migration for existing databases.
            cursor = await db.execute("PRAGMA table_info(members)")
            existing_columns = {row[1] for row in await cursor.fetchall()}
            if "warera_user_id" not in existing_columns:
                await db.execute("ALTER TABLE members ADD COLUMN warera_user_id TEXT")
            if "assigned_role" not in existing_columns:
                await db.execute("ALTER TABLE members ADD COLUMN assigned_role TEXT")
            await db.commit()

        self.add_view(TicketPanelView())

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Commands synced to server {GUILD_ID}")
        else:
            await self.tree.sync()
            print("Global commands synced (may take time to appear).")

    async def on_ready(self):
        print(f"Bot online: {self.user} (ID: {self.user.id})")

    async def on_member_join(self, member: discord.Member):
        role = discord.utils.get(
            member.guild.roles, name=UNVERIFIED_ROLE
        )

        if role is None:
            print(
                f"Missing role '{UNVERIFIED_ROLE}' "
                f"in server {member.guild.name}"
            )
            return

        try:
            await member.add_roles(
                role, reason="New member onboarding"
            )
        except discord.Forbidden:
            print("Cannot assign role. Check role hierarchy/permissions.")
            return

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                INSERT OR IGNORE INTO members (discord_id)
                VALUES (?)
            """, (member.id,))
            await db.commit()


bot = WarEraBot()


def get_role(guild: discord.Guild, name: str):
    return discord.utils.get(guild.roles, name=name)


def has_named_role(member: discord.Member, role_name: str) -> bool:
    return any(role.name == role_name for role in member.roles)


async def resolve_warera_username(username: str):
    """Resolve one exact WarEra username to a public user object.

    Uses the documented search_users endpoint when exposed by the installed
    WarEra client. Refuses ambiguous or non-exact matches.
    """
    search_namespace = getattr(warera, "search", None)
    search_users = getattr(search_namespace, "search_users", None) if search_namespace else None
    if search_users is None:
        raise RuntimeError(
            "Your installed WarEra Python client does not expose search.search_users. "
            "Update/install the supported warera-client package before using /link."
        )

    result = await search_users(username)
    # The search endpoint normally returns user IDs. Support model/dict wrappers too.
    if hasattr(result, "model_dump"):
        result = result.model_dump(mode="json")
    if isinstance(result, dict):
        candidates = result.get("items") or result.get("users") or result.get("results") or []
    else:
        candidates = (getattr(result, "items", None) or getattr(result, "users", None)
                      or getattr(result, "results", None) or result)
        candidates = candidates if isinstance(candidates, (list, tuple)) else []

    user_ids = []
    for item in candidates:
        if isinstance(item, str):
            user_ids.append(item)
        elif isinstance(item, dict):
            uid = item.get("id") or item.get("user_id") or item.get("userId")
            if uid:
                user_ids.append(str(uid))
        else:
            uid = getattr(item, "id", None)
            if uid:
                user_ids.append(str(uid))

    # If search returned a single user object rather than IDs, inspect it directly.
    direct_users = []
    if isinstance(result, dict) and (result.get("username") or result.get("name")):
        direct_users.append(result)

    matches = []
    for user_id in dict.fromkeys(user_ids):
        user = await warera.user.get_by_id(user_id)
        data = user.model_dump(mode="json") if hasattr(user, "model_dump") else {}
        found_name = str(data.get("username") or data.get("name") or "")
        if found_name.casefold() == username.casefold():
            matches.append((user_id, found_name))
    for data in direct_users:
        found_name = str(data.get("username") or data.get("name") or "")
        user_id = data.get("id") or data.get("user_id") or data.get("userId")
        if user_id and found_name.casefold() == username.casefold():
            matches.append((str(user_id), found_name))

    # De-duplicate by ID. Never choose a fuzzy or ambiguous result.
    unique = {uid: name for uid, name in matches}
    if not unique:
        return None
    if len(unique) > 1:
        raise ValueError("More than one exact match was returned. Ask the applicant for their profile URL/ID.")
    return next(iter(unique.items()))


async def finalize_verification(guild: discord.Guild, member: discord.Member, role: discord.Role, actor: discord.Member):
    """Assign the chosen role, remove Unverified, and archive the active application."""
    unverified = get_role(guild, UNVERIFIED_ROLE)
    bot_member = guild.me
    if bot_member is None or role >= bot_member.top_role or (unverified and unverified >= bot_member.top_role):
        raise RuntimeError("Move the bot's highest role above the selected role and Unverified role.")

    if unverified and unverified in member.roles:
        await member.remove_roles(unverified, reason=f"Verified by {actor}")
    if role not in member.roles:
        await member.add_roles(role, reason=f"WarEra account linked and verification completed by {actor}")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE members SET verified = 1, assigned_role = ? WHERE discord_id = ?", (role.name, member.id))
        cursor = await db.execute("SELECT channel_id, created_at FROM tickets WHERE discord_id = ?", (member.id,))
        ticket_record = await cursor.fetchone()

    archive_note = ""
    if ticket_record:
        thread = guild.get_thread(ticket_record[0])
        if thread is not None:
            safe_name = ''.join(ch.lower() if ch.isalnum() else '-' for ch in member.display_name)
            safe_name = '-'.join(part for part in safe_name.split('-') if part)[:40] or 'member'
            try:
                try:
                    await thread.remove_user(member)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                await thread.edit(name=f"verified-{safe_name}"[:100], archived=True, locked=True, reason=f"Verification completed by {actor}")
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "INSERT INTO ticket_history (discord_id, thread_id, status, created_at, closed_by) VALUES (?, ?, 'approved', ?, ?)",
                        (member.id, thread.id, ticket_record[1], actor.id)
                    )
                    await db.execute("DELETE FROM tickets WHERE discord_id = ?", (member.id,))
                    await db.commit()
                archive_note = " The private application thread was archived for staff records."
            except discord.HTTPException as error:
                print(f"Could not archive verification thread {ticket_record[0]}: {error}")
                archive_note = " The role was assigned, but the thread could not be archived; check bot permissions."
    return archive_note


async def create_verification_ticket(interaction: discord.Interaction):
    """Create a private thread for one applicant in the public ticket-panel channel."""
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Please open a ticket from inside the server.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    member = interaction.user
    team_role = get_role(guild, VERIFICATION_TEAM_ROLE)
    verified_role = get_role(guild, VERIFIED_ROLE)

    if team_role is None:
        await interaction.followup.send(
            f"The **{VERIFICATION_TEAM_ROLE}** role is missing. Ask an administrator to create it.",
            ephemeral=True
        )
        return

    if verified_role and verified_role in member.roles:
        await interaction.followup.send(
            "You already have the Verified role. If you need help, contact a moderator.",
            ephemeral=True
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT channel_id FROM tickets WHERE discord_id = ?", (member.id,)
        )
        existing = await cursor.fetchone()

    if existing:
        existing_thread = guild.get_thread(existing[0])
        if existing_thread is not None and not existing_thread.archived:
            await interaction.followup.send(
                f"You already have an open verification thread: {existing_thread.mention}",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none()
            )
            return
        # If the saved thread was deleted or archived unexpectedly, clear the active-ticket pointer.
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM tickets WHERE discord_id = ?", (member.id,))
            await db.commit()

    panel_channel = discord.utils.get(guild.text_channels, name=TICKET_PANEL_CHANNEL)
    if panel_channel is None:
        await interaction.followup.send(
            f"I couldn't find **#{TICKET_PANEL_CHANNEL}**. Run `/setup_ticket_panel` first.",
            ephemeral=True
        )
        return

    safe_name = ''.join(ch.lower() if ch.isalnum() else '-' for ch in member.display_name)
    safe_name = '-'.join(part for part in safe_name.split('-') if part)[:40] or 'member'

    try:
        thread = await panel_channel.create_thread(
            name=f"verify-{safe_name}",
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=1440,
            reason=f"Verification thread opened by {member} ({member.id})"
        )
        await thread.add_user(member)

        # Discord threads cannot be granted to a role as a whole. Add each current
        # Verification Team member individually so only the applicant and staff are participants.
        for staff_member in team_role.members:
            if not staff_member.bot:
                try:
                    await thread.add_user(staff_member)
                except (discord.Forbidden, discord.HTTPException):
                    print(f"Could not add verifier {staff_member} to thread {thread.id}")

    except discord.Forbidden:
        await interaction.followup.send(
            "I couldn't create the private thread. Check that the bot has View Channel, "
            "Send Messages, Create Private Threads, Send Messages in Threads, and Manage Threads "
            "permissions in #open-a-ticket.",
            ephemeral=True
        )
        return
    except discord.HTTPException as error:
        print(f"Private verification thread creation failed: {error}")
        await interaction.followup.send(
            "Discord couldn't create the verification thread. Check the bot terminal and channel permissions.",
            ephemeral=True
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO tickets (discord_id, channel_id) VALUES (?, ?)",
            (member.id, thread.id)
        )
        await db.commit()

    await thread.send(
        f"Welcome {member.mention}! This is your **private verification thread**.\n\n"
        "**Please send these details here:**\n"
        "1. Your WarEra profile URL (for example, `https://app.warera.io/user/PLAYER_ID`).\n"
        "2. Your Indian state or Union Territory.\n"
        "3. Your preferred language(s).\n\n"
        f"A member of **{VERIFICATION_TEAM_ROLE}** will review your application. "
        "Please don't post sensitive documents or private information here. "
        "A public profile link alone does not prove account ownership; a verifier may ask for reasonable proof.\n\n"
        "Only you and the invited verification staff can see this private thread. "
        "After approval, it will be renamed and archived for staff records.",
        allowed_mentions=discord.AllowedMentions(users=True, roles=False)
    )
    await interaction.followup.send(
        f"Your private verification thread is ready: {thread.mention}",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none()
    )

@bot.tree.command(
    name="setup_ticket_panel",
    description="Create the verification category, channels, and ticket panel"
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True, manage_roles=True)
async def setup_ticket_panel(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("Use this command inside your server.", ephemeral=True)
        return

    team_role = get_role(guild, VERIFICATION_TEAM_ROLE)
    if team_role is None:
        await interaction.response.send_message(
            f"Create the **{VERIFICATION_TEAM_ROLE}** role first, then run this command again.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    bot_member = guild.me
    category_overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        team_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        ),
    }
    if bot_member is not None:
        category_overwrites[bot_member] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            manage_channels=True
        )

    category = discord.utils.get(guild.categories, name=VERIFICATION_CATEGORY)
    if category is None:
        category = await guild.create_category(
            VERIFICATION_CATEGORY, overwrites=category_overwrites,
            reason="Set up verification ticket system"
        )

    panel_channel = discord.utils.get(category.text_channels, name=TICKET_PANEL_CHANNEL)
    if panel_channel is None:
        panel_overwrites = dict(category_overwrites)
        panel_overwrites[guild.default_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=False, read_message_history=True,
            create_public_threads=False, create_private_threads=False,
            send_messages_in_threads=False
        )
        panel_channel = await guild.create_text_channel(
            TICKET_PANEL_CHANNEL, category=category, overwrites=panel_overwrites,
            reason="Create verification ticket panel"
        )
    else:
        # Keep the panel readable by everyone, but prevent public messages/threads.
        await panel_channel.set_permissions(
            guild.default_role,
            view_channel=True,
            send_messages=False,
            read_message_history=True,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=False,
            reason="Keep verification panel read-only"
        )

    logs_channel = discord.utils.get(category.text_channels, name=VERIFICATION_LOG_CHANNEL)
    if logs_channel is None:
        logs_channel = await guild.create_text_channel(
            VERIFICATION_LOG_CHANNEL, category=category,
            overwrites=category_overwrites,
            reason="Create private verification logs channel"
        )

    embed = discord.Embed(
        title="WarEra Verification",
        description=(
            "Ready to join the community? Open a private verification ticket below.\n\n"
            "You'll be asked for your WarEra profile link, state/UT, and preferred language. "
            "A verifier will review your application before access is granted."
        ),
        color=discord.Color.dark_gold()
    )
    embed.set_footer(text="One open ticket per member")
    await panel_channel.send(embed=embed, view=TicketPanelView())

    await interaction.followup.send(
        f"Ticket system is ready. Panel: {panel_channel.mention} | Logs: {logs_channel.mention}",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none()
    )


@bot.tree.command(
    name="verify",
    description="Start the server verification process"
)
@app_commands.guild_only()
async def verify(interaction: discord.Interaction):
    await interaction.response.send_message(
        "Your verification request has started! "
        "Please contact a server moderator for approval. "
        "Once approved, you will receive the Verified role.",
        ephemeral=True
    )


@bot.tree.command(
    name="recommend",
    description="Recommend an applicant for Admin review"
)
@app_commands.guild_only()
@app_commands.describe(member="Applicant to recommend", note="Brief verification notes")
async def recommend(interaction: discord.Interaction, member: discord.Member, note: str):
    if not isinstance(interaction.user, discord.Member) or not has_named_role(interaction.user, VERIFICATION_TEAM_ROLE):
        await interaction.response.send_message("Only members of the Verification Team can submit recommendations.", ephemeral=True)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT channel_id FROM tickets WHERE discord_id = ?", (member.id,))
        active = await cursor.fetchone()
        if not active:
            await interaction.response.send_message("This member has no active verification application.", ephemeral=True)
            return
        await db.execute(
            "INSERT INTO verification_recommendations (discord_id, recommended_by, note) VALUES (?, ?, ?) "
            "ON CONFLICT(discord_id) DO UPDATE SET recommended_by = excluded.recommended_by, note = excluded.note, created_at = CURRENT_TIMESTAMP",
            (member.id, interaction.user.id, note[:1000])
        )
        await db.commit()
    await interaction.response.send_message(f"Recommendation saved for {member.mention}. An Admin can now review and link the account.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(
    name="link",
    description="Link a member's WarEra account and complete verification"
)
@app_commands.guild_only()
@app_commands.describe(member="Discord member", username="Exact WarEra username", role="Role to assign after linking")
@app_commands.choices(role=[
    app_commands.Choice(name="Indian Citizen", value="Indian Citizen"),
    app_commands.Choice(name="NRI", value="NRI"),
])
async def link(interaction: discord.Interaction, member: discord.Member, username: str, role: app_commands.Choice[str]):
    if not isinstance(interaction.user, discord.Member) or not has_named_role(interaction.user, ADMIN_ROLE):
        await interaction.response.send_message("Only a member with the Admin role can use /link.", ephemeral=True)
        return
    if member.bot:
        await interaction.response.send_message("Bot accounts cannot be linked through verification.", ephemeral=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT note, recommended_by FROM verification_recommendations WHERE discord_id = ?", (member.id,))
        recommendation = await cursor.fetchone()
    if not recommendation:
        await interaction.response.send_message("This applicant has not yet been recommended by the Verification Team. Use /recommend first.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        resolved = await resolve_warera_username(username.strip())
    except Exception as error:
        print(f"WarEra username search failed: {type(error).__name__}: {error}")
        await interaction.followup.send(f"I couldn't safely resolve that username. {error}", ephemeral=True)
        return
    if resolved is None:
        await interaction.followup.send("No exact WarEra username match found. Check the spelling or use the applicant's profile URL/ID workflow instead.", ephemeral=True)
        return

    warera_id, exact_username = resolved
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT discord_id FROM members WHERE warera_user_id = ? AND discord_id != ?", (warera_id, member.id))
        already_linked = await cursor.fetchone()
        if already_linked:
            await interaction.followup.send("That WarEra account is already linked to another Discord member. No changes were made.", ephemeral=True)
            return
        await db.execute(
            "INSERT INTO members (discord_id, verified, warera_username, warera_user_id, assigned_role) VALUES (?, 0, ?, ?, ?) "
            "ON CONFLICT(discord_id) DO UPDATE SET warera_username = excluded.warera_username, warera_user_id = excluded.warera_user_id, assigned_role = excluded.assigned_role",
            (member.id, exact_username, warera_id, role.value)
        )
        await db.commit()

    selected_role = get_role(interaction.guild, role.value)
    if selected_role is None:
        await interaction.followup.send(f"The WarEra account was linked, but the **{role.value}** Discord role doesn't exist. Create it, then run /link again.", ephemeral=True)
        return
    try:
        archive_note = await finalize_verification(interaction.guild, member, selected_role, interaction.user)
    except discord.Forbidden:
        await interaction.followup.send("The account was linked, but Discord denied role changes. Check the bot's Manage Roles permission and role hierarchy.", ephemeral=True)
        return
    except RuntimeError as error:
        await interaction.followup.send(f"The account was linked, but verification couldn't finish: {error}", ephemeral=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM verification_recommendations WHERE discord_id = ?", (member.id,))
        await db.commit()
    await interaction.followup.send(f"✅ Linked **{exact_username}** to {member.mention}, assigned **{role.value}**, and completed verification.{archive_note}", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(
    name="approve",
    description="Legacy command; use /link to complete verification"
)
@app_commands.guild_only()
async def approve(interaction: discord.Interaction, member: discord.Member):
    if not isinstance(interaction.user, discord.Member) or not has_named_role(interaction.user, ADMIN_ROLE):
        await interaction.response.send_message("Only a member with the Admin role can use verification administration commands.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"Use `/link` for {member.mention}. Verification now requires a saved Verification Team recommendation, a successful WarEra account match, and an Admin-selected role.",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
    )


@bot.tree.command(
    name="application_history",
    description="View staff-only verification application history for a member"
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_roles=True)
async def application_history(
    interaction: discord.Interaction,
    member: discord.Member
):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT thread_id, status, created_at, closed_at, closed_by "
            "FROM ticket_history WHERE discord_id = ? ORDER BY id DESC LIMIT 10",
            (member.id,)
        )
        rows = await cursor.fetchall()
        active_cursor = await db.execute(
            "SELECT channel_id, created_at FROM tickets WHERE discord_id = ?",
            (member.id,)
        )
        active = await active_cursor.fetchone()

    embed = discord.Embed(
        title=f"Verification history: {member.display_name}",
        color=discord.Color.dark_gold()
    )
    if active:
        active_thread = interaction.guild.get_thread(active[0])
        active_text = active_thread.mention if active_thread else f"Thread ID: {active[0]} (not currently cached)"
        embed.add_field(name="Active application", value=active_text, inline=False)
    if rows:
        lines = []
        for thread_id, status, created_at, closed_at, closed_by in rows:
            thread = interaction.guild.get_thread(thread_id)
            thread_text = thread.mention if thread else f"Thread ID: {thread_id}"
            lines.append(
                f"• **{status.title()}** — {thread_text}\n"
                f"  Opened: {created_at}; closed: {closed_at}; staff ID: {closed_by or 'unknown'}"
            )
        embed.add_field(name="Past applications (latest 10)", value="\n".join(lines)[:1024], inline=False)
    if not active and not rows:
        embed.description = "No application history is recorded for this member yet."

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="profile",
    description="View a member's Discord and WarEra link status"
)
@app_commands.guild_only()
async def profile(
    interaction: discord.Interaction,
    member: discord.Member = None
):
    target = member or interaction.user

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT verified, warera_username, warera_user_id, assigned_role
            FROM members
            WHERE discord_id = ?
        """, (target.id,))
        record = await cursor.fetchone()

    verified_role = get_role(interaction.guild, VERIFIED_ROLE)
    is_verified = bool(
        record and record[0]
    ) or bool(
        verified_role and verified_role in target.roles
    )

    username = record[1] if record else None
    warera_user_id = record[2] if record else None
    assigned_role = record[3] if record else None

    embed = discord.Embed(
        title="WarEra Member Profile",
        color=discord.Color.green() if is_verified
        else discord.Color.orange()
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(
        name="Discord member",
        value=target.mention,
        inline=False
    )
    embed.add_field(
        name="Server verification",
        value="✅ Verified" if is_verified
        else "⏳ Not verified",
        inline=True
    )
    embed.add_field(
        name="WarEra account",
        value=(f"[{username}](https://app.warera.io/user/{warera_user_id})" if username and warera_user_id else username or "Not linked"),
        inline=True
    )
    if assigned_role:
        embed.add_field(name="Assigned server role", value=assigned_role, inline=True)

    if username:
        embed.set_footer(text="WarEra username matched through the public search API; this does not independently prove account ownership.")
    else:
        embed.set_footer(text="WarEra account not linked yet.")

    await interaction.response.send_message(embed=embed)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        message = "You do not have the required role or permission to use this command."
    else:
        print(f"Command error: {error}")
        message = "Something went wrong. Check the bot terminal."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(
            message, ephemeral=True
        )


if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is missing. Check your .env file."
    )



@bot.tree.command(
    name="warera",
    description="Look up a WarEra player's public stats"
)
@app_commands.describe(
    player_url="The player's WarEra profile URL"
)
async def warera_profile(
    interaction: discord.Interaction,
    player_url: str
):
    import re

    match = re.fullmatch(
        r"https?://app\.warera\.io/user/([a-fA-F0-9]{24})/?",
        player_url.strip()
    )

    if not match:
        await interaction.response.send_message(
            "Please provide a valid WarEra profile URL, "
            "like https://app.warera.io/user/YOUR_PLAYER_ID",
            ephemeral=True
        )
        return

    player_id = match.group(1)

    await interaction.response.defer()

    try:
        player = await warera.user.get_by_id(player_id)
        data = player.model_dump(mode="json")

        leveling = data.get("leveling") or {}
        stats = data.get("stats") or {}
        wealth = stats.get("wealth") or {}
        rankings = data.get("rankings") or {}
        skills = data.get("skills") or {}

        damage_rank = rankings.get("user_damages") or {}
        wealth_rank = rankings.get("user_wealth") or {}

        embed = discord.Embed(
            title="WarEra Player Profile",
            url=f"https://app.warera.io/user/{player_id}",
            color=discord.Color.dark_gold()
        )

        embed.add_field(
            name="Level",
            value=str(leveling.get("level", "Unknown")),
            inline=True
        )
        embed.add_field(
            name="Military Rank",
            value=str(data.get("military_rank", "Unknown")),
            inline=True
        )
        embed.add_field(
            name="Total Damage",
            value=f"{stats.get('damages_count', 0):,}",
            inline=True
        )
        embed.add_field(
            name="Damage Ranking",
            value=f"#{damage_rank.get('rank', 'N/A')}",
            inline=True
        )
        
        embed.add_field(
            name="Total Wealth",
            value=f"{wealth.get('total', 0):,.2f}",
            inline=True
)

        
        embed.add_field(
            name="Wealth Ranking",
            value=f"#{wealth_rank.get('rank', 'N/A')}",
            inline=True
        )

        for skill_name in ("attack", "energy", "health"):
            skill = skills.get(skill_name) or {}
            embed.add_field(
                name=skill_name.replace("_", " ").title(),
                value=str(skill.get("total", "N/A")),
                inline=True
            )

        embed.add_field(
            name="Country ID",
            value=str(data.get("country") or "Unknown"),
            inline=False
        )
        embed.add_field(
            name="Military Unit ID",
            value=str(data.get("mu") or "None"),
            inline=False
        )

        avatar = data.get("avatar_url")
        if avatar:
            embed.set_thumbnail(url=avatar)

        embed.set_footer(text="WarEra API • Public profile lookup")

        await interaction.followup.send(embed=embed)

    except Exception as error:
        print(
            f"WarEra profile lookup failed: "
            f"{type(error).__name__}: {error}"
        )
        await interaction.followup.send(
            "Couldn't retrieve that WarEra profile. "
            "Check the URL and try again.",
            ephemeral=True
        )

    

bot.run(TOKEN)
