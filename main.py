#!/usr/bin/env python3
"""
Cleaned Leaderboard Bot
- Weekly automatic leaderboard every Sunday at 10:00 AM IST
- Commands:
  /setup-auto-leaderboard channel role top from_channel
  /test-leaderboard
  /leaderboard-timer
  /show-shard-id
- Persistent config saved to 'leaderboard_data.json'
"""

import os
import json
import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, Any

import pytz
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ----- Configuration -----
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
CONFIG_FILE = "leaderboard_data.json"

# Timezone configuration
IST_TZ = pytz.timezone("Asia/Kolkata")
UTC_TZ = pytz.utc
TARGET_HOUR = 10
TARGET_MINUTE = 0
TARGET_DAY = 6  # Sunday (Monday=0 .. Sunday=6)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("leaderboard_bot")

# Intents
# Required intents for guilds, members (for role management), and message content (for counting)
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True


def now_utc() -> datetime:
    """Returns the current timezone-aware UTC datetime."""
    return datetime.now(UTC_TZ)


def iso_to_dt(iso_str: str) -> datetime:
    """Convert ISO string to timezone-aware UTC datetime."""
    dt = datetime.fromisoformat(iso_str)
    # Ensure datetime object is timezone-aware and converted to UTC
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC_TZ)
    return dt.astimezone(UTC_TZ)


def dt_to_iso(dt: datetime) -> str:
    """Convert timezone-aware datetime to ISO string (UTC)."""
    return dt.astimezone(UTC_TZ).isoformat()


class LeaderboardBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.leaderboard_config: Dict[int, Dict[str, Any]] = {}
        # Load persisted config immediately
        self._load_config()

    # ---------- Persistence ----------
    def _load_config(self) -> None:
        """Load JSON config file into self.leaderboard_config."""
        if not os.path.exists(CONFIG_FILE):
            self.leaderboard_config = {}
            return

        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not read config file; starting with empty config. Error: %s", e)
            self.leaderboard_config = {}
            return

        parsed: Dict[int, Dict[str, Any]] = {}
        for guild_id_str, cfg in raw.items():
            try:
                gid = int(guild_id_str)
            except ValueError:
                continue

            # Convert next_run_dt from string to datetime object on load
            local_cfg = cfg.copy()
            nr = local_cfg.get("next_run_dt")
            if isinstance(nr, str):
                try:
                    local_cfg["next_run_dt"] = iso_to_dt(nr)
                except Exception:
                    local_cfg["next_run_dt"] = None
            parsed[gid] = local_cfg

        self.leaderboard_config = parsed
        logger.info("Loaded configuration for %d guild(s).", len(self.leaderboard_config))

    def _save_config(self) -> None:
        """Serialize current config to JSON (convert datetimes to ISO strings)."""
        serializable: Dict[str, Dict[str, Any]] = {}
        for gid, cfg in self.leaderboard_config.items():
            copy_cfg = cfg.copy()
            nr = copy_cfg.get("next_run_dt")
            if isinstance(nr, datetime):
                copy_cfg["next_run_dt"] = dt_to_iso(nr)
            serializable[str(gid)] = copy_cfg

        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(serializable, f, indent=4)
            logger.info("Configuration saved.")
        except OSError as e:
            logger.exception("Failed to save configuration: %s", e)

    # ---------- Scheduling utility ----------
    def get_next_sunday_10am(self, from_dt: datetime) -> datetime:
        """
        Given a timezone-aware datetime (preferably UTC), return the next Sunday at 10:00 AM IST,
        expressed as a UTC timezone-aware datetime.
        """
        # 1. Ensure starting datetime is IST for calculation
        start_ist = from_dt.astimezone(IST_TZ)

        # 2. Calculate days until next Sunday (TARGET_DAY=6)
        days_ahead = TARGET_DAY - start_ist.weekday()
        if days_ahead <= 0:
            days_ahead += 7

        # 3. Set the date, time, and timezone for the next target Sunday
        next_sunday_ist = (start_ist + timedelta(days=days_ahead)).replace(
            hour=TARGET_HOUR, minute=TARGET_MINUTE, second=0, microsecond=0
        )

        # 4. If the calculation somehow returns a time that is in the past, push it to the week after.
        if next_sunday_ist <= start_ist:
            next_sunday_ist += timedelta(days=7)

        # 5. Return the result in UTC (for comparison with now_utc())
        return next_sunday_ist.astimezone(UTC_TZ)

    # ---------- Bot lifecycle ----------
    async def setup_hook(self) -> None:
        # Start the background task immediately in setup_hook
        if not self.weekly_leaderboard_task.is_running():
            self.weekly_leaderboard_task.start()
            logger.info("Weekly leaderboard background task started.")

    async def on_ready(self) -> None:
        """
        Runs when the bot is connected. We sync commands here to ensure they are available
        immediately upon connection, preventing the CommandSignatureMismatch error.
        """
        if self.user:
            logger.info("Logged in as %s (ID: %s). Bot ready.", self.user, self.user.id)
        
        # Global sync of application commands
        try:
            await self.tree.sync()
            logger.info("Slash commands synced successfully on ready.")
        except Exception as e:
            logger.error("Failed to sync slash commands: %s", e)


    # ---------- Core leaderboard logic ----------
    async def _run_leaderboard_logic(self, guild_id: int, config: Dict[str, Any], interaction: discord.Interaction | None = None) -> bool:
        """
        Fetch messages, compute top members, manage roles, and post the leaderboard message.
        """
        guild = self.get_guild(guild_id)
        if not guild:
            logger.warning("Guild %s not in bot cache.", guild_id)
            return False

        channel = guild.get_channel(config.get("channel_id"))
        role = guild.get_role(config.get("role_id"))
        from_channel = guild.get_channel(config.get("from_channel_id"))
        top_n = int(config.get("top", 3))

        # 1. Configuration Check
        if not all([channel, role, from_channel]):
            logger.warning("Configuration incomplete for guild %s. channel/role/from_channel not found.", guild_id)
            return False
            
        # 2. Permission and Role Hierarchy Check
        if not guild.me.guild_permissions.manage_roles:
            logger.warning("Bot lacks manage_roles permission in guild %s. Cannot run leaderboard.", guild_id)
            return False
            
        if guild.me.top_role.position <= role.position:
            logger.warning("Bot's top role is not high enough to manage role %s in guild %s.", role.name, guild_id)
            if interaction:
                 # Only respond to user if this was a manual test command
                 await interaction.followup.send(f"❌ Test run failed: Bot's role is lower than or equal to the role {role.name}. Please adjust role hierarchy.", ephemeral=True)
            return False

        # 3. Message Fetching and Counting
        seven_days_ago = now_utc() - timedelta(days=7)
        message_counts = Counter()

        try:
            # Ensure the channel type supports history()
            if not isinstance(from_channel, discord.TextChannel):
                 logger.error("from_channel %s is not a TextChannel.", from_channel.id)
                 return False

            async for message in from_channel.history(after=seven_days_ago, limit=None):
                if not message.author.bot:
                    message_counts[message.author.id] += 1
        except discord.Forbidden:
            logger.exception("Error fetching history: Bot lacks Read Message History in %s", from_channel.name)
            return False
        except Exception as e:
            logger.exception("Error fetching history in guild %s: %s", guild_id, e)
            return False

        top_members = message_counts.most_common(top_n)

        # 4. Role Removal (Previous Winners)
        current_role_holders = [m for m in role.members if m.guild.id == guild_id]
        for member in current_role_holders:
            try:
                await member.remove_roles(role, reason="Weekly Leaderboard Reset")
            except Exception:
                logger.exception("Failed to remove role from %s in guild %s", member.display_name, guild_id)

        # 5. Role Assignment (New Winners)
        top_users_objects = []
        for user_id, cnt in top_members:
            member = guild.get_member(user_id)
            if member:
                try:
                    await member.add_roles(role, reason="Weekly Leaderboard Winner")
                except Exception:
                    logger.exception("Failed to add role to %s in guild %s", member.display_name, guild_id)
                top_users_objects.append((member, cnt))

        # 6. Build the Leaderboard Message
        def get_user_data(idx: int):
            if idx < len(top_users_objects):
                member, cnt = top_users_objects[idx]
                return member.mention, cnt
            return "N/A", 0

        u1, c1 = get_user_data(0)
        u2, c2 = get_user_data(1)
        u3, c3 = get_user_data(2)

        # NOTE: The custom emoji and user IDs are left as placeholders from the original
        message_content = (
            f"Hello fellas, \n"
            f"We're back with the weekly leaderboard update!! <:Pika_Think:1444211873687011328>\n"
            f"Here are the top {top_n} active members past week:\n"
            f":first_place: Top 1: {u1} with more than {c1} messages. \n"
            f"-# Gets 50k unb in cash\n"
            f":second_place: Top 2: {u2} with more than {c2} messages.\n"
            f"-# Gets 25k unb in cash\n"
            f":third_place: Top 3: {u3} with more than {c3} messages.\n"
            f"-# Gets 10k unb in cash\n\n"
            f"All of the top three members have been granted the role:\n"
            f"{role.mention}\n\n"
            f"Top 1 can change their server nickname once. Top 1 & 2 can have a custom role with name and colour based on their requests. Contact <@1193415556402008169> (<@&1405157360045002785>) within 24 hours to claim your awards."
        )

        # 7. Post the Message
        try:
            if not isinstance(channel, discord.TextChannel):
                logger.error("Target channel %s is not a TextChannel.", channel.id)
                return False
                
            await channel.send(message_content)
        except discord.HTTPException as e:
            logger.exception("Failed to send leaderboard message to channel %s in guild %s: %s", channel.id, guild_id, e)
            return False

        # 8. Update Schedule and Persist
        next_run = self.get_next_sunday_10am(now_utc())
        self.leaderboard_config[guild_id]["next_run_dt"] = next_run
        self._save_config()

        logger.info("Leaderboard executed for guild %s. Next run: %s IST", guild_id, next_run.astimezone(IST_TZ).strftime("%Y-%m-%d %H:%M:%S %Z"))
        return True

    # ---------- Background task ----------
    @tasks.loop(minutes=30) # Runs every 30 minutes to ensure responsiveness near the target time
    async def weekly_leaderboard_task(self) -> None:
        """
        Runs frequently and checks if any guild's next_run_dt has passed.
        """
        await self.wait_until_ready()

        if not self.leaderboard_config:
            return

        now = now_utc()
        for guild_id, cfg in list(self.leaderboard_config.items()):
            target = cfg.get("next_run_dt")
            
            # Recalculate if target is invalid or in the past
            if not isinstance(target, datetime) or target.tzinfo is None or target < now - timedelta(hours=1):
                target = self.get_next_sunday_10am(now)
                cfg["next_run_dt"] = target
                self._save_config()

            # Check if the target time has been reached
            if now >= target:
                logger.info("Triggering scheduled leaderboard for guild %s", guild_id)
                # The logic automatically updates the next_run_dt on success
                success = await self._run_leaderboard_logic(guild_id, cfg)
                if not success:
                    logger.warning("Scheduled run for guild %s failed; will retry next cycle.", guild_id)

    @weekly_leaderboard_task.before_loop
    async def before_weekly_leaderboard_task(self) -> None:
        """
        Ensures the bot is ready before starting the loop.
        """
        await self.wait_until_ready()
        
        # This initial wait isn't strictly necessary with the 30-minute loop,
        # but provides a safety buffer.
        logger.info("Starting background task checks after short delay.")
        await asyncio.sleep(10) 

    # ---------- Application Commands ----------
    @app_commands.command(name="show-shard-id", description="Displays the shard ID for the current server.")
    async def cmd_show_shard_id(self, interaction: discord.Interaction) -> None:
        if interaction.guild:
            shard_id = interaction.guild.shard_id
            await interaction.response.send_message(
                f"The shard ID for this server (`{interaction.guild.name}`) is: `{shard_id}`.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message("This command must be run in a guild.", ephemeral=True)

    @app_commands.command(name="setup-auto-leaderboard", description="Configure the bot to run the leaderboard automatically every Sunday at 10 AM IST")
    @app_commands.describe(
        channel="The channel where the leaderboard message will be sent",
        role="The role to give to the top active members",
        top="How many members to fetch (e.g., 3)",
        from_channel="The channel to count messages from"
    )
    async def cmd_setup_auto_leaderboard(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        role: discord.Role,
        top: int,
        from_channel: discord.TextChannel,
    ) -> None:
        # Permission checks
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You need Administrator permissions to set up the leaderboard.", ephemeral=True)
            return

        if top < 1 or top > 50:
            await interaction.response.send_message("The 'top' value must be between 1 and 50.", ephemeral=True)
            return
            
        # Bot role hierarchy check before deferring
        if interaction.guild and interaction.guild.me.top_role.position <= role.position:
            await interaction.response.send_message(f"❌ Bot's role is lower than or equal to the role {role.name}. Please ensure the bot's role is higher in the hierarchy to manage this role.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild_id = interaction.guild_id
        next_run = self.get_next_sunday_10am(now_utc())

        self.leaderboard_config[guild_id] = {
            "channel_id": channel.id,
            "role_id": role.id,
            "top": top,
            "from_channel_id": from_channel.id,
            "next_run_dt": next_run,
        }
        self._save_config()

        await interaction.followup.send(
            f"✅ Automated Weekly Leaderboard Setup Complete!\n"
            f"The leaderboard is now scheduled to run every **Sunday at 10:00 AM IST**.\n"
            f"The first scheduled run is on: {discord.utils.format_dt(next_run, 'F')} ({discord.utils.format_dt(next_run, 'R')}).",
            ephemeral=True,
        )

    @app_commands.command(name="test-leaderboard", description="Immediately run the configured leaderboard logic.")
    async def cmd_test_leaderboard(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You need Administrator permissions to run a test leaderboard.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        if guild_id not in self.leaderboard_config:
            await interaction.followup.send("❌ Leaderboard has not been set up for this server. Please run `/setup-auto-leaderboard` first.", ephemeral=True)
            return

        cfg = self.leaderboard_config[guild_id]
        
        # The role hierarchy check is now handled inside _run_leaderboard_logic and gives feedback.
        success = await self._run_leaderboard_logic(guild_id, cfg, interaction)

        if success:
            nxt = cfg.get("next_run_dt")
            ch = interaction.guild.get_channel(cfg["channel_id"])
            await interaction.followup.send(
                f"✅ Test run complete. Leaderboard sent to {ch.mention} and roles managed.\n"
                f"The next *scheduled* run is now set for: {discord.utils.format_dt(nxt, 'F')}.",
                ephemeral=True,
            )
        else:
            # If logic failed but no specific error was sent back, send a generic error
            if not interaction.response.is_done():
                 await interaction.followup.send("❌ Test run failed. Check bot permissions (View Channel, Read History, Manage Roles) and configuration.", ephemeral=True)

    @app_commands.command(name="leaderboard-timer", description="Shows the remaining time until the next automatic leaderboard run.")
    async def cmd_leaderboard_timer(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id not in self.leaderboard_config:
            await interaction.response.send_message("❌ The automatic leaderboard has not been set up yet. Use `/setup-auto-leaderboard`.", ephemeral=True)
            return

        cfg = self.leaderboard_config[guild_id]
        next_run = cfg.get("next_run_dt")
        
        if not isinstance(next_run, datetime) or next_run.tzinfo is None:
            # Fix invalid/missing next_run date
            new_next_run = self.get_next_sunday_10am(now_utc())
            cfg["next_run_dt"] = new_next_run
            self._save_config()
            next_run = new_next_run
            
        now = now_utc()
        remaining = next_run - now
        
        if remaining.total_seconds() <= 0:
            # If run time passed, the background task will compute the next one on its next cycle.
            msg = (
                f"🚨 The last scheduled run time was {discord.utils.format_dt(next_run, 'F')} ({discord.utils.format_dt(next_run, 'R')}).\n"
                f"The task should run on the next background check (within 30 minutes). Use `/test-leaderboard` to run it now."
            )
        else:
            msg = (
                f"⏳ The next automatic leaderboard update is scheduled for **Sunday at 10:00 AM IST**.\n"
                f"The exact time is: {discord.utils.format_dt(next_run, 'F')} ({discord.utils.format_dt(next_run, 'R')})."
            )

        await interaction.response.send_message(msg, ephemeral=True)


# ----- Register app commands onto the tree and Run the bot -----
bot = LeaderboardBot()

# These commands are already decorated inside the class, but explicitly adding them
# ensures they are included in the tree if they weren't automatically picked up.
bot.tree.add_command(bot.cmd_show_shard_id)
bot.tree.add_command(bot.cmd_setup_auto_leaderboard)
bot.tree.add_command(bot.cmd_test_leaderboard)
bot.tree.add_command(bot.cmd_leaderboard_timer)


if __name__ == "__main__":
    if not TOKEN:
        logger.error("Error: DISCORD_TOKEN environment variable not set. Please create a .env file.")
    else:
        try:
            bot.run(TOKEN)
        except discord.errors.LoginFailure:
            logger.error("Error: Bot failed to log in. Check your DISCORD_TOKEN in the .env file.")
        except Exception as e:
            logger.exception("An unexpected error occurred while running the bot: %s", e)