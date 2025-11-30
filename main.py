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
from typing import Dict, Any, Optional

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


def now_utc() -> datetime:
    """Returns the current timezone-aware UTC datetime."""
    return datetime.now(UTC_TZ)


def iso_to_dt(iso_str: str) -> Optional[datetime]:
    """Convert ISO string to timezone-aware UTC datetime."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC_TZ)
        return dt.astimezone(UTC_TZ)
    except ValueError:
        return None


def dt_to_iso(dt: datetime) -> str:
    """Convert timezone-aware datetime to ISO string (UTC)."""
    return dt.astimezone(UTC_TZ).isoformat()


class LeaderboardBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.leaderboard_config: Dict[int, Dict[str, Any]] = {}
        self._load_config()

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

            local_cfg = cfg.copy()
            nr = local_cfg.get("next_run_dt")
            if isinstance(nr, str):
                parsed_dt = iso_to_dt(nr)
                local_cfg["next_run_dt"] = parsed_dt if parsed_dt is not None else None
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
        except OSError as e:
            logger.error("Failed to save configuration: %s", e)

    def get_next_sunday_10am(self, from_dt: datetime) -> datetime:
        """Calculate the next Sunday at 10:00 AM IST as a UTC datetime."""
        start_ist = from_dt.astimezone(IST_TZ)
        days_ahead = TARGET_DAY - start_ist.weekday()
        if days_ahead <= 0:
            days_ahead += 7

        next_sunday_ist = (start_ist + timedelta(days=days_ahead)).replace(
            hour=TARGET_HOUR, minute=TARGET_MINUTE, second=0, microsecond=0
        )

        if next_sunday_ist <= start_ist:
            next_sunday_ist += timedelta(days=7)

        return next_sunday_ist.astimezone(UTC_TZ)

    async def setup_hook(self) -> None:
        if not self.weekly_leaderboard_task.is_running():
            self.weekly_leaderboard_task.start()
            logger.info("Weekly leaderboard background task started.")

    async def on_ready(self) -> None:
        if self.user:
            logger.info("Logged in as %s (ID: %s).", self.user, self.user.id)
        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} command(s).")
        except Exception as e:
            logger.error("Failed to sync slash commands: %s", e)

    async def _run_leaderboard_logic(self, guild_id: int, config: Dict[str, Any], interaction: Optional[discord.Interaction] = None) -> bool:
        guild = self.get_guild(guild_id)
        if not guild:
            logger.warning("Guild %s not in bot cache.", guild_id)
            return False

        channel = guild.get_channel(config.get("channel_id"))
        role = guild.get_role(config.get("role_id"))
        from_channel = guild.get_channel(config.get("from_channel_id"))

        if not all([channel, role, from_channel]):
            logger.warning("Configuration incomplete for guild %s.", guild_id)
            return False

        if not guild.me.guild_permissions.manage_roles:
            logger.warning("Bot lacks manage_roles permission in guild %s.", guild_id)
            return False

        if guild.me.top_role.position <= role.position:
            logger.warning("Bot's top role is not high enough to manage role %s.", role.name)
            if interaction:
                try:
                    await interaction.followup.send(
                        f"❌ Cannot run leaderboard: Bot's role is lower than or equal to the role {role.name}. "
                        "Please adjust the role hierarchy so the bot's highest role is above this role.",
                        ephemeral=True
                    )
                except discord.HTTPException:
                    pass
            return False

        # Count messages from the past 7 days
        seven_days_ago = now_utc() - timedelta(days=7)
        message_counts = Counter()

        try:
            if not isinstance(from_channel, discord.TextChannel):
                logger.error("from_channel %s is not a TextChannel.", from_channel.id)
                return False

            async for message in from_channel.history(after=seven_days_ago, limit=None):
                if not message.author.bot:
                    message_counts[message.author.id] += 1

        except discord.Forbidden:
            logger.error("Bot lacks permission to read message history in channel %s.", from_channel.name)
            return False
        except Exception as e:
            logger.error("Error fetching message history in guild %s: %s", guild_id, e)
            return False

        top_n = int(config.get("top", 3))
        top_members = message_counts.most_common(top_n)

        # Remove role from current holders
        current_role_holders = [m for m in role.members if m.guild.id == guild_id]
        for member in current_role_holders:
            try:
                await member.remove_roles(role, reason="Weekly Leaderboard Reset")
            except discord.HTTPException:
                logger.error("Failed to remove role from %s.", member.display_name)

        # Assign role to new winners
        top_users_objects = []
        for user_id, count in top_members:
            member = guild.get_member(user_id)
            if member:
                try:
                    await member.add_roles(role, reason="Weekly Leaderboard Winner")
                    top_users_objects.append((member, count))
                except discord.HTTPException:
                    logger.error("Failed to add role to %s.", member.display_name)

        # Build and send leaderboard message
        def get_user_data(idx: int):
            if idx < len(top_users_objects):
                member, cnt = top_users_objects[idx]
                return member.mention, cnt
            return "No participant", 0

        u1, c1 = get_user_data(0)
        u2, c2 = get_user_data(1)
        u3, c3 = get_user_data(2)

        message_content = (
            f"Hello fellas,\n"
            f"Here are the top {top_n} active members from the past week:\n\n"
            f":first_place: 1st Place: {u1} with {c1} messages\n"
            f":second_place: 2nd Place: {u2} with {c2} messages\n"
            f":third_place: 3rd Place: {u3} with {c3} messages\n\n"
            f"All top members have been assigned the {role.mention} role.\n\n"
            f"Prize distribution:\n"
            f"• 1st Place: 50k unb in cash, one-time nickname change, custom role\n"
            f"• 2nd Place: 25k unb in cash, custom role\n"
            f"• 3rd Place: 10k unb in cash\n\n"
            f"Please contact <@1193415556402008169> within 24 hours to claim your rewards."
        )

        try:
            if isinstance(channel, discord.TextChannel):
                await channel.send(message_content)
            else:
                logger.error("Target channel %s is not a TextChannel.", channel.id)
                return False
        except discord.HTTPException as e:
            logger.error("Failed to send leaderboard message: %s", e)
            return False

        # Schedule next run
        next_run = self.get_next_sunday_10am(now_utc())
        self.leaderboard_config[guild_id]["next_run_dt"] = next_run
        self._save_config()

        logger.info("Leaderboard successfully executed for guild %s. Next run: %s", 
                   guild_id, next_run.astimezone(IST_TZ).strftime("%Y-%m-%d %H:%M:%S IST"))
        return True

    @tasks.loop(minutes=30)
    async def weekly_leaderboard_task(self) -> None:
        now = now_utc()
        for guild_id, cfg in list(self.leaderboard_config.items()):
            target = cfg.get("next_run_dt")
            if not isinstance(target, datetime) or target.tzinfo is None or target < now:
                target = self.get_next_sunday_10am(now)
                cfg["next_run_dt"] = target
                self._save_config()

            if now >= target:
                logger.info("Executing scheduled leaderboard for guild %s", guild_id)
                await self._run_leaderboard_logic(guild_id, cfg)

    @weekly_leaderboard_task.before_loop
    async def before_weekly_leaderboard_task(self) -> None:
        await self.wait_until_ready()

    @app_commands.command(name="setup-auto-leaderboard", description="Configure automatic weekly leaderboards")
    @app_commands.describe(
        channel="Channel where leaderboard messages will be posted",
        role="Role to assign to top leaderboard participants",
        top="Number of top participants to track (1-10)",
        from_channel="Channel from which to count messages"
    )
    async def cmd_setup_auto_leaderboard(
        self, interaction: discord.Interaction,
        channel: discord.TextChannel,
        role: discord.Role,
        top: app_commands.Range[int, 1, 10],
        from_channel: discord.TextChannel
    ):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("You need Administrator permissions to configure leaderboards.", ephemeral=True)
            return

        if interaction.guild and interaction.guild.me.top_role.position <= role.position:
            await interaction.response.send_message(
                f"Bot's highest role must be above the role '{role.name}' in the role hierarchy.",
                ephemeral=True
            )
            return

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

        await interaction.response.send_message(
            f"✅ Automatic weekly leaderboards have been configured.\n"
            f"Leaderboards will be posted every Sunday at 10:00 AM IST in {channel.mention}.\n"
            f"Next scheduled run: {discord.utils.format_dt(next_run, 'F')}",
            ephemeral=True
        )

    @app_commands.command(name="test-leaderboard", description="Manually run the configured leaderboard")
    async def cmd_test_leaderboard(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Administrator permissions are required to test leaderboards.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        if guild_id not in self.leaderboard_config:
            await interaction.response.send_message(
                "Leaderboards have not been configured for this server. Use /setup-auto-leaderboard first.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        success = await self._run_leaderboard_logic(guild_id, self.leaderboard_config[guild_id], interaction)

        if success:
            next_run = self.leaderboard_config[guild_id].get("next_run_dt")
            await interaction.followup.send(
                f"✅ Leaderboard test completed successfully.\n"
                f"Next scheduled automatic run: {discord.utils.format_dt(next_run, 'F')}",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "❌ The leaderboard test failed. Check the bot logs for detailed error information.",
                ephemeral=True
            )

    @app_commands.command(name="leaderboard-timer", description="Show time until next scheduled leaderboard")
    async def cmd_leaderboard_timer(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        if guild_id not in self.leaderboard_config:
            await interaction.response.send_message(
                "Automatic leaderboards have not been configured for this server.",
                ephemeral=True
            )
            return

        cfg = self.leaderboard_config[guild_id]
        next_run = cfg.get("next_run_dt")

        if not isinstance(next_run, datetime) or next_run.tzinfo is None:
            next_run = self.get_next_sunday_10am(now_utc())
            cfg["next_run_dt"] = next_run
            self._save_config()

        now = now_utc()
        if now >= next_run:
            await interaction.response.send_message(
                "The next scheduled leaderboard run time has passed. "
                "It will be executed on the next background task cycle or you can run /test-leaderboard.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"The next automatic leaderboard is scheduled for {discord.utils.format_dt(next_run, 'F')} "
                f"({discord.utils.format_dt(next_run, 'R')}).",
                ephemeral=True
            )

    @app_commands.command(name="show-shard-id", description="Display the shard ID for this server")
    async def cmd_show_shard_id(self, interaction: discord.Interaction):
        if interaction.guild:
            shard_id = interaction.guild.shard_id
            await interaction.response.send_message(f"This server is on shard {shard_id}.", ephemeral=True)
        else:
            await interaction.response.send_message("This command can only be used in servers.", ephemeral=True)


bot = LeaderboardBot()

if __name__ == "__main__":
    if not TOKEN:
        logger.error("DISCORD_TOKEN is not set. Please create a .env file with DISCORD_TOKEN.")
    else:
        bot.run(TOKEN)