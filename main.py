import discord
from discord import app_commands, Intents, Client
from discord.ext import tasks 
from datetime import datetime, timedelta, timezone
import asyncio
import os
import json # Used for local file persistence
import collections
from typing import Optional

# --- Configuration for Persistence ---
# File to store the bot's configuration (timers, channel IDs, role IDs)
CONFIG_FILE = 'leaderboard_config.json'

# --- Bot Setup ---

# Set your bot token here. Using an environment variable is best practice.
TOKEN = os.environ.get('DISCORD_BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE') 
if TOKEN == 'YOUR_BOT_TOKEN_HERE':
    print("WARNING: Please set the DISCORD_BOT_TOKEN environment variable or replace 'YOUR_BOT_TOKEN_HERE' with your actual bot token.")

# Intents required:
intents = Intents.default()
intents.messages = True
intents.message_content = True 
intents.guilds = True
intents.members = True # MANDATORY for role assignment

class LeaderboardClient(Client):
    def __init__(self, *, intents: Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.config: Optional[dict] = None # Stores the loaded configuration

    async def on_ready(self):
        await self.tree.sync()
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        await self.load_config()
        # Start the background task after loading the config
        self.leaderboard_scheduler.start() 

    async def load_config(self):
        """Loads configuration from local JSON file."""
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r') as f:
                    self.config = json.load(f)
                    print(f"Configuration loaded from {CONFIG_FILE}.")
            else:
                self.config = None
                print(f"No configuration file found at {CONFIG_FILE}.")
        except Exception as e:
            print(f"Error loading config from file: {e}")
            self.config = None

    async def save_config(self):
        """Saves current configuration to local JSON file."""
        if self.config is None:
            return # Don't save if config is empty

        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.config, f, indent=4)
            print(f"Configuration saved to {CONFIG_FILE}.")
        except Exception as e:
            print(f"Error saving config to file: {e}")

    # --- Utility Functions ---

    def get_next_sunday_430am_gmt(self):
        """Calculates the timestamp for the next Sunday at 4:30 AM UTC (GMT)."""
        now = datetime.now(timezone.utc)
        
        # Calculate days until next Sunday (Sunday is weekday 6, Monday is 0)
        days_to_sunday = (6 - now.weekday() + 7) % 7
        if days_to_sunday == 0:
            # If it's already Sunday, check if 4:30 AM has passed
            if now.hour > 4 or (now.hour == 4 and now.minute >= 30):
                # Target time passed, schedule for next Sunday
                days_to_sunday = 7
        
        # Calculate the next Sunday date
        next_sunday = now + timedelta(days=days_to_sunday)
        
        # Set the time to 4:30 AM UTC
        target_time = next_sunday.replace(hour=4, minute=30, second=0, microsecond=0, tzinfo=timezone.utc)
        
        # If the target time is in the past (only possible if days_to_sunday was 0), push to next week
        if target_time <= now:
            target_time += timedelta(weeks=1)
            
        return target_time.timestamp()

    async def run_leaderboard_job(self, guild_id, target_channel_id, source_channel_id, role_id, top_count, is_test=False):
        """
        Core logic to fetch messages, calculate top users, assign roles, and send the message.
        """
        guild = self.get_guild(guild_id)
        if not guild:
            print(f"Error: Guild with ID {guild_id} not found. (Perhaps bot was removed from guild?)")
            return

        target_channel = guild.get_channel(target_channel_id)
        source_channel = guild.get_channel(source_channel_id)
        role = guild.get_role(role_id)

        if not target_channel or not source_channel or not role:
            error_message = f"Job failed: Missing resources. "
            if not target_channel: error_message += "Target Channel not found. "
            if not source_channel: error_message += "Source Channel not found. "
            if not role: error_message += "Role not found. "
            print(error_message)
            if is_test and target_channel:
                 await target_channel.send(f"❌ Leaderboard setup failed. Please check if the configured channels and role still exist. Details: {error_message}")
            return

        # 1. Calculate time range (Past 7 days)
        seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
        message_counts = collections.defaultdict(int)
        
        # 2. Fetch messages and count
        
        status_channel = target_channel # The channel to send status/error updates
        
        if is_test:
            await status_channel.send(f"⏳ Starting leaderboard calculation from {source_channel.mention} for the past 7 days...")

        try:
            # Fetch history
            # The limit=None argument fetches all messages since the 'after' date (7 days ago)
            async for message in source_channel.history(limit=None, after=seven_days_ago):
                # Ignore messages from bots
                if not message.author.bot:
                    message_counts[message.author] += 1
            
            # Sort and get top users
            sorted_users = sorted(message_counts.items(), key=lambda item: item[1], reverse=True)
            top_members = sorted_users[:top_count]

        except discord.errors.Forbidden:
            error_msg = f"❌ Error: Bot does not have permissions to read history in {source_channel.mention}."
            print(error_msg)
            await status_channel.send(error_msg)
            return
        except Exception as e:
            error_msg = f"❌ An unexpected error occurred during message fetching: {e}"
            print(error_msg)
            await status_channel.send(error_msg)
            return

        # 3. Role Management (Clear and Assign)
        
        # Get members who currently have the role
        members_with_role = [member for member in guild.members if role in member.roles]

        # Clear role from all current holders
        for member in members_with_role:
            try:
                await member.remove_roles(role, reason="Weekly leaderboard role reset.")
                # Small sleep to respect Discord rate limits
                await asyncio.sleep(0.5) 
            except discord.HTTPException as e:
                print(f"Could not remove role from {member.display_name}: {e}")

        # Assign role to top members
        top_member_objects = []
        for member_obj, _ in top_members:
            # Get the full Member object
            full_member = guild.get_member(member_obj.id)
            if full_member:
                top_member_objects.append(full_member)
                try:
                    await full_member.add_roles(role, reason="Weekly leaderboard top member award.")
                    await asyncio.sleep(0.5)
                except discord.HTTPException as e:
                    print(f"Could not assign role to {full_member.display_name}: {e}")

        # 4. Format and Send Leaderboard Message
        
        leaderboard_entries = []
        emoji_map = {0: ":first_place:", 1: ":second_place:", 2: ":third_place:"}
        
        # Prepare the list of winners for the template
        for i in range(top_count):
            if i < len(top_members):
                member, count = top_members[i]
                
                # Customize based on position, matching the user's requested template
                if i == 0:
                    award = "-# Gets 50k unb in cash"
                elif i == 1:
                    award = "-# Gets 25k unb in cash"
                elif i == 2:
                    award = "-# Gets 10k unb in cash"
                else:
                    award = f"-# Gets a consolation prize."

                # Use member.mention and the count of messages
                leaderboard_entries.append(
                    f"{emoji_map.get(i, f'#{i+1}:')} {member.mention} with **{count}** messages.\n{award}"
                )
            else:
                leaderboard_entries.append(f"#{i+1}: Not enough data/members this week.")
                
        leaderboard_text = "\n".join(leaderboard_entries)

        # Assemble the final message
        final_message = f"""
Hello fellas, 
We're back with the weekly leaderboard update!! <:Pika_Think:1444211873687011328>

Here are the top {top_count} active members past week–
{leaderboard_text}

All of the top members have been granted the role:
**{role.name}**

Top 1 can change their server nickname once. Top 1 & 2 can have a custom role with name and colour based on their requests. Contact <@1193415556402008169> (<@&1405157360045002785>) within 24 hours to claim your awards.
        """
        
        # Send the final message
        await target_channel.send(final_message)
        
        if is_test:
            await target_channel.send("✅ Test run complete. Roles have been updated and the message was sent.")
            
        print(f"Leaderboard job executed successfully in Guild {guild.id}.")


    # --- Background Task Scheduler ---

    @tasks.loop(minutes=10) # Check every 10 minutes (or less, depending on desired precision)
    async def leaderboard_scheduler(self):
        await self.wait_until_ready()
        
        # 1. Load config if not loaded (for persistence after restart)
        if self.config is None:
             await self.load_config()
             if self.config is None:
                # Still no config, skip this check cycle
                return 
        
        # 2. Check for required configuration keys
        required_keys = ["next_run_timestamp_gmt", "leaderboard_channel_id", "source_channel_id", "top_user_role_id", "top_users_count", "guild_id"]
        if not all(k in self.config for k in required_keys):
            print("Scheduler waiting for full configuration via /setup-auto-leaderboard.")
            return

        # 3. Check timer
        next_run_ts = self.config['next_run_timestamp_gmt']
        # Convert the stored timestamp back to a datetime object in UTC
        next_run_dt = datetime.fromtimestamp(next_run_ts, timezone.utc)
        now = datetime.now(timezone.utc)
        
        if now >= next_run_dt:
            print(f"Scheduled job running now: {now.isoformat()}. Target was: {next_run_dt.isoformat()}")
            
            # Run the job
            await self.run_leaderboard_job(
                guild_id=self.config['guild_id'],
                target_channel_id=self.config['leaderboard_channel_id'],
                source_channel_id=self.config['source_channel_id'],
                role_id=self.config['top_user_role_id'],
                top_count=self.config['top_users_count'],
                is_test=False
            )
            
            # Update the next run time and save (ensures restart resilience)
            self.config['next_run_timestamp_gmt'] = self.get_next_sunday_430am_gmt()
            await self.save_config()
        # else: bot is sleeping, timer is correct
                

    # --- Slash Commands ---
    
    @app_commands.command(name="setup-auto-leaderboard", description="Set up the automatic weekly message activity leaderboard.")
    @app_commands.describe(
        channel="The channel where the leaderboard message will be sent.",
        role="The role to be cleared and reassigned to top members.",
        top="The number of top users to fetch (e.g., 3).",
        from_channel="The channel from which to count messages (the activity channel)."
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_auto_leaderboard(self, interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role, top: app_commands.Range[int, 1, 10], from_channel: discord.TextChannel):
        """Sets up the configuration and initial timer."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        # 1. Calculate the initial next run time
        next_run_ts = self.get_next_sunday_430am_gmt()
        next_run_dt = datetime.fromtimestamp(next_run_ts, timezone.utc)

        # 2. Store configuration
        self.config = {
            "guild_id": interaction.guild_id,
            "leaderboard_channel_id": channel.id,
            "top_user_role_id": role.id,
            "top_users_count": top,
            "source_channel_id": from_channel.id,
            "next_run_timestamp_gmt": next_run_ts,
        }
        await self.save_config()

        await interaction.followup.send(
            f"✅ **Leaderboard setup complete!**\n"
            f"• Leaderboard will be posted to: {channel.mention}\n"
            f"• Message activity will be counted from: {from_channel.mention}\n"
            f"• Top **{top}** members will receive the role: **{role.name}**.\n"
            f"• Next run scheduled for: **{next_run_dt.strftime('%A, %Y-%m-%d at %H:%M UTC (GMT)')}**.",
            ephemeral=True
        )

    @app_commands.command(name="test-leaderboard", description="Manually run the leaderboard job right now in this channel.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def test_leaderboard(self, interaction: discord.Interaction):
        """Runs the job immediately using the stored configuration."""
        await interaction.response.defer(thinking=True)
        
        if not self.config:
            await self.load_config()

        if not self.config or not self.config.get("leaderboard_channel_id"):
            await interaction.followup.send("❌ Leaderboard setup is incomplete. Please run `/setup-auto-leaderboard` first.", ephemeral=True)
            return

        # Use the current interaction channel ID as the target channel ID for the test run
        test_config = self.config.copy()
        test_config['leaderboard_channel_id'] = interaction.channel_id
        
        await self.run_leaderboard_job(
            guild_id=test_config['guild_id'],
            target_channel_id=test_config['leaderboard_channel_id'],
            source_channel_id=test_config['source_channel_id'],
            role_id=test_config['top_user_role_id'],
            top_count=test_config['top_users_count'],
            is_test=True
        )
        
        await interaction.followup.send("Test job initiated. Check the channel for results.", ephemeral=True)


    @app_commands.command(name="timer-leaderboard", description="Shows the time remaining until the next automatic leaderboard update.")
    async def timer_leaderboard(self, interaction: discord.Interaction):
        """Calculates and displays the time remaining until the next run."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        if not self.config:
            await self.load_config()

        if not self.config or not self.config.get("next_run_timestamp_gmt"):
            await interaction.followup.send("❌ Leaderboard setup is incomplete. Please run `/setup-auto-leaderboard` first.", ephemeral=True)
            return

        next_run_ts = self.config['next_run_timestamp_gmt']
        next_run_dt = datetime.fromtimestamp(next_run_ts, timezone.utc)
        now = datetime.now(timezone.utc)
        
        if next_run_dt <= now:
            await interaction.followup.send(
                "⏳ The leaderboard job is currently overdue or running. It will be scheduled for the following Sunday (4:30 AM GMT) shortly after completion."
            )
            return

        time_left: timedelta = next_run_dt - now
        
        days = time_left.days
        hours = time_left.seconds // 3600
        minutes = (time_left.seconds % 3600) // 60
        seconds = time_left.seconds % 60
        
        countdown_msg = (
            f"📅 **Time until next automatic leaderboard update:**\n"
            f"It is scheduled for **{next_run_dt.strftime('%A, %Y-%m-%d at %H:%M UTC (GMT)')}**.\n"
            f"Time remaining: `{days} days, {hours} hours, {minutes} minutes, {seconds} seconds`."
        )

        await interaction.followup.send(countdown_msg, ephemeral=True)


# --- Execution ---

bot = LeaderboardClient(intents=intents)

# Run the bot (Token must be provided)
if TOKEN and TOKEN != 'YOUR_BOT_TOKEN_HERE':
    bot.run(TOKEN)
else:
    print("Bot execution skipped. Please provide a valid DISCORD_BOT_TOKEN.")