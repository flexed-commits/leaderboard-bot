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
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.guilds = True


def now_utc() -> datetime:
    return datetime.now(UTC_TZ)


def iso_to_dt(iso_str: str) -> datetime:
    """Convert ISO string to timezone-aware UTC datetime."""
    # datetime.fromisoformat returns naive or offset-aware. We will ensure UTC tzinfo.
    dt = datetime.fromisoformat(iso_str)
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
        # Load persisted config immediately (but converting datetimes will be done in setup_hook)
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

            # Ensure keys exist and convert next_run_dt if it's a string
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
        if from_dt.tzinfo is None:
            from_dt = from_dt.replace(tzinfo=UTC_TZ)
        # Convert to IST
        start_ist = from_dt.astimezone(IST_TZ)

        days_ahead = TARGET_DAY - start_ist.weekday()
        if days_ahead <= 0:
            days_ahead += 7

        next_sunday_ist = (start_ist + timedelta(days=days_ahead)).replace(
            hour=TARGET_HOUR, minute=TARGET_MINUTE, second=0, microsecond=0
        )

        # If this somehow ends up <= start_ist, push another week
        if next_sunday_ist <= start_ist:
            next_sunday_ist += timedelta(days=7)

        # Return in UTC
        return next_sunday_ist.astimezone(UTC_TZ)

    # ---------- Bot lifecycle ----------
    async def setup_hook(self) -> None:
        # Sync application commands
        await self.tree.sync()
        logger.info("Slash commands synced.")

        # Convert any stored ISO strings to datetime (if not already done)
        for gid, cfg in self.leaderboard_config.items():
            nr = cfg.get("next_run_dt")
            if isinstance(nr, str):
                try:
                    self.leaderboard_config[gid]["next_run_dt"] = iso_to_dt(nr)
                except Exception:
                    self.leaderboard_config[gid]["next_run_dt"] = None

        # Ensure at least one scheduled loop is started
        if not self.weekly_leaderboard_task.is_running():
            self.weekly_leaderboard_task.start()
            logger.info("Weekly leaderboard background task started.")

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (ID: %s). Bot ready.", self.user, self.user.id)

    # ---------- Core leaderboard logic ----------
    async def _run_leaderboard_logic(self, guild_id: int, config: Dict[str, Any], interaction: discord.Interaction | None = None) -> bool:
        """
        Fetch messages from the configured channel for the past 7 days, compute top members,
        assign/remove role, post leaderboard message, persist next run time.
        """
        guild = self.get_guild(guild_id)
        if not guild:
            logger.warning("Guild %s not in bot cache.", guild_id)
            return False

        channel = guild.get_channel(config.get("channel_id"))
        role = guild.get_role(config.get("role_id"))
        from_channel = guild.get_channel(config.get("from_channel_id"))
        top_n = int(config.get("top", 3))

        if not all([channel, role, from_channel]):
            logger.warning("Configuration incomplete for guild %s. channel/role/from_channel not found.", guild_id)
            return False

        # Permission sanity: ability to manage roles
        if not guild.me.guild_permissions.manage_roles:
            logger.warning("Bot lacks manage_roles permission in guild %s.", guild_id)
            return False

        seven_days_ago = now_utc() - timedelta(days=7)
        message_counts = Counter()

        try:
            async for message in from_channel.history(after=seven_days_ago, limit=None):
                if not message.author.bot:
                    message_counts[message.author.id] += 1
        except Exception as e:
            logger.exception("Error fetching history in guild %s channel %s: %s", guild_id, from_channel.id if from_channel else None, e)
            return False

        top_members = message_counts.most_common(top_n)

        # Remove role from current holders (safe removal)
        current_role_holders = [m for m in role.members if m.guild.id == guild_id]
        for member in current_role_holders:
            # Only try to remove if the bot can manage the member's roles
            if guild.me.top_role.position > role.position:
                try:
                    await member.remove_roles(role, reason="Weekly Leaderboard Reset")
                except discord.Forbidden:
                    logger.warning("Forbidden to remove role from %s in guild %s", member.display_name, guild_id)
                except Exception:
                    logger.exception("Failed to remove role from %s in guild %s", member.display_name, guild_id)

        top_users_objects = []
        for user_id, cnt in top_members:
            member = guild.get_member(user_id)
            if member:
                # Assign role if bot can
                if guild.me.top_role.position > role.position:
                    try:
                        await member.add_roles(role, reason="Weekly Leaderboard Winner")
                    except discord.Forbidden:
                        logger.warning("Forbidden to add role to %s in guild %s", member.display_name, guild_id)
                    except Exception:
                        logger.exception("Failed to add role to %s in guild %s", member.display_name, guild_id)
                top_users_objects.append((member, cnt))

        # Build the message
        def get_user_data(idx: int):
            if idx < len(top_users_objects):
                member, cnt = top_users_objects[idx]
                return member.mention, cnt
            return "N/A", 0

        u1, c1 = get_user_data(0)
        u2, c2 = get_user_data(1)
        u3, c3 = get_user_data(2)

        message_content = (
            f"Hello fellas, \n"
            f"We're back with the weekly leaderboard update!! <:Pika_Think:1444211873687011328>\n"
            f"Here are the top {top_n} active members past week–\n"
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

        try:
            await channel.send(message_content)
        except discord.HTTPException as e:
            logger.exception("Failed to send leaderboard message to channel %s in guild %s: %s", channel.id, guild_id, e)
            return False

        # Persist next run time
        next_run = self.get_next_sunday_10am(now_utc())
        self.leaderboard_config[guild_id]["next_run_dt"] = next_run
        self._save_config()

        logger.info("Leaderboard executed for guild %s. Next run: %s IST", guild_id, next_run.astimezone(IST_TZ).strftime("%Y-%m-%d %H:%M:%S %Z"))
        return True

    # ---------- Background task ----------
    @tasks.loop(seconds=3600)
    async def weekly_leaderboard_task(self) -> None:
        """
        Runs hourly and checks if any guild's next_run_dt has passed. If so, runs the leaderboard.
        The initial wait/delay is handled in before_weekly_leaderboard_task.
        """
        # Ensure the bot is ready
        await self.wait_until_ready()

        if not self.leaderboard_config:
            return

        now = now_utc()
        for guild_id, cfg in list(self.leaderboard_config.items()):
            target = cfg.get("next_run_dt")
            if not isinstance(target, datetime):
                # calculate and persist a valid next_run
                target = self.get_next_sunday_10am(now)
                cfg["next_run_dt"] = target
                self._save_config()

            if now >= target:
                logger.info("Triggering scheduled leaderboard for guild %s", guild_id)
                success = await self._run_leaderboard_logic(guild_id, cfg)
                if not success:
                    logger.warning("Scheduled run for guild %s failed; will retry next cycle.", guild_id)

    @weekly_leaderboard_task.before_loop
    async def before_weekly_leaderboard_task(self) -> None:
        """
        This runs once before the loop's first iteration. We use it to compute the initial
        delay until the earliest scheduled next_run_dt among configured guilds (or a short default).
        """
        await self.wait_until_ready()

        # If no guilds configured yet, wait a short default time
        if not self.leaderboard_config:
            logger.info("No leaderboard configurations found on startup. Waiting 60s before first loop iteration.")
            await asyncio.sleep(60)
            return

        # Find the earliest next_run_dt among guilds, ensure it's valid
        now = now_utc()
        earliest = None
        for cfg in self.leaderboard_config.values():
            nr = cfg.get("next_run_dt")
            if not isinstance(nr, datetime) or nr < now:
                # compute a fresh next run for that guild
                nr = self.get_next_sunday_10am(now)
                cfg["next_run_dt"] = nr
                self._save_config()
            if earliest is None or nr < earliest:
                earliest = nr

        # Compute delay until earliest run. Ensure minimum 60s to let bot finish start-up.
        delay_seconds = max(60, (earliest - now).total_seconds()) if earliest else 60
        logger.info("Initial delay before first scheduled check: %.1f seconds (until %s IST)", delay_seconds, earliest.astimezone(IST_TZ).strftime("%Y-%m-%d %H:%M:%S %Z") if earliest else "N/A")

        await asyncio.sleep(delay_seconds)

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

        # If the task isn't running, start it. Usually it will already be running via setup_hook.
        if not self.weekly_leaderboard_task.is_running():
            self.weekly_leaderboard_task.start()

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
            await interaction.followup.send("❌ Test run failed. Check bot permissions and configuration.", ephemeral=True)

    @app_commands.command(name="leaderboard-timer", description="Shows the remaining time until the next automatic leaderboard run.")
    async def cmd_leaderboard_timer(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id not in self.leaderboard_config:
            await interaction.response.send_message("❌ The automatic leaderboard has not been set up yet. Use `/setup-auto-leaderboard`.", ephemeral=True)
            return

        cfg = self.leaderboard_config[guild_id]
        next_run = cfg.get("next_run_dt")
        if not isinstance(next_run, datetime):
            await interaction.response.send_message("❌ Configuration error: Next run time is missing. Please run `/setup-auto-leaderboard` again.", ephemeral=True)
            return

        now = now_utc()
        remaining = next_run - now
        if remaining.total_seconds() <= 0:
            msg = (
                f"🚨 The scheduled run was missed on {discord.utils.format_dt(next_run, 'F')} ({discord.utils.format_dt(next_run, 'R')}).\n"
                f"The task should run on the next background cycle (within the hour). Use `/test-leaderboard` to force it now."
            )
        else:
            msg = (
                f"⏳ The next automatic leaderboard update is scheduled for **Sunday at 10:00 AM IST**.\n"
                f"The exact time is: {discord.utils.format_dt(next_run, 'F')} ({discord.utils.format_dt(next_run, 'R')})."
            )

        await interaction.response.send_message(msg, ephemeral=True)


# ----- Register app commands onto the tree -----
# We register after creating the bot instance to attach these into the tree.
bot = LeaderboardBot()

# Attach the commands to the bot tree (since we defined them as methods with decorators, we need to add them)
bot.tree.add_command(bot.cmd_show_shard_id)
bot.tree.add_command(bot.cmd_setup_auto_leaderboard)
bot.tree.add_command(bot.cmd_test_leaderboard)
bot.tree.add_command(bot.cmd_leaderboard_timer)

# ----- Run the bot -----
if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN not found in environment. Please check your .env file.")
    else:
        try:
            bot.run(TOKEN)
        except discord.errors.LoginFailure:
            print("Error: Bot failed to log in. Check your DISCORD_TOKEN in the .env file.")
        except Exception as e:
            print(f"An unexpected error occurred while running the bot: {e}")