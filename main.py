import discord
from discord import app_commands
from discord.ext import commands, tasks
import datetime
from collections import Counter
import asyncio
import os
import json
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pytz # Used for named timezones - requires: pip install pytz

# --- CONFIGURATION ---
# IMPORTANT: Ensure your DISCORD_TOKEN is set in a .env file
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
CONFIG_FILE = 'leaderboard_data.json' # File to store persistent data

# Timezone and Target Time Setup (IST - India Standard Time)
IST_TZ = pytz.timezone("Asia/Kolkata") 
UTC_TZ = pytz.utc                     
TARGET_HOUR = 10  # 10 AM
TARGET_MINUTE = 0 # 0 minutes
TARGET_DAY = 6    # Sunday (Monday is 0, Sunday is 6)

# Initialize Bot with necessary intents
intents = discord.Intents.default()
intents.members = True          
intents.message_content = True  
intents.guilds = True

class LeaderboardBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.leaderboard_config = self._load_config()

    def _load_config(self):
        """Loads configuration and timer state from the JSON file."""
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f:
                try:
                    loaded_config = json.load(f)
                    # Convert keys from string (JSON requirement) back to int
                    return {int(k): v for k, v in loaded_config.items()}
                except json.JSONDecodeError:
                    print(f"Warning: {CONFIG_FILE} is corrupted. Starting fresh.")
                    return {}
        return {}

    def _save_config(self):
        """Saves configuration and timer state to the JSON file."""
        with open(CONFIG_FILE, 'w') as f:
            # We convert datetime objects back to ISO format strings for JSON storage
            serializable_config = {}
            for guild_id, config in self.leaderboard_config.items():
                serializable_config[str(guild_id)] = config.copy() # Keys must be strings for JSON
                # Ensure we don't save the datetime object itself, but the string representation
                if 'next_run_dt' in serializable_config[str(guild_id)]:
                    dt = serializable_config[str(guild_id)]['next_run_dt']
                    if isinstance(dt, datetime):
                        serializable_config[str(guild_id)]['next_run_dt'] = dt.isoformat()

            json.dump(serializable_config, f, indent=4)

    def get_next_sunday_10am(self, start_time: datetime) -> datetime:
        """Calculates the date and time of the next Sunday at 10:00 AM IST."""

        # 1. Localize the start time to IST
        start_time_utc = start_time.astimezone(UTC_TZ)
        start_time_ist = start_time_utc.astimezone(IST_TZ)

        # 2. Calculate days until next Sunday (0=Monday, 6=Sunday)
        days_ahead = TARGET_DAY - start_time_ist.weekday()
        if days_ahead <= 0:  # If today is Sunday or past Sunday, aim for next week
            days_ahead += 7

        # 3. Calculate the date of the next Sunday
        next_sunday = start_time_ist + timedelta(days=days_ahead)

        # 4. Set the time to 10:00 AM IST (preserving the IST timezone)
        next_run_dt = next_sunday.replace(
            hour=TARGET_HOUR, 
            minute=TARGET_MINUTE, 
            second=0, 
            microsecond=0
        )

        # 5. Check if the calculated time is in the past (e.g., if it's Sunday and 11 AM IST)
        if next_run_dt < start_time_ist:
            next_run_dt += timedelta(days=7)

        # Return as UTC for consistent internal use
        return next_run_dt.astimezone(UTC_TZ) 

    async def setup_hook(self):
        """Executed before the bot connects, used for syncing commands and resuming tasks."""
        await self.tree.sync()
        print("Slash commands synced!")

        # Load and potentially convert the stored run time strings back to datetime objects
        for guild_id, config in self.leaderboard_config.items():
            if 'next_run_dt' in config and isinstance(config['next_run_dt'], str):
                try:
                    # Convert stored ISO string back to timezone-aware datetime object (using UTC)
                    config['next_run_dt'] = datetime.fromisoformat(config['next_run_dt']).replace(tzinfo=UTC_TZ)
                except ValueError:
                    print(f"Error loading datetime for guild {guild_id}. Resetting schedule.")
                    config['next_run_dt'] = None

        self._start_weekly_task_with_persistence()
        print("Weekly leaderboard task initialized/resumed.")

    def _start_weekly_task_with_persistence(self):
        """
        Calculates the time until the first configured guild's next scheduled run,
        and cleanly stops/starts the task with that delay.
        """
        if not self.leaderboard_config:
            if not self.weekly_leaderboard_task.is_running():
                 # Use a short initial delay if no setup yet
                 self.weekly_leaderboard_task.start(delay=60) 
            return

        # We base the initial delay calculation on the first configured guild
        guild_id = next(iter(self.leaderboard_config))
        config = self.leaderboard_config[guild_id]

        target_dt_utc = config.get('next_run_dt')

        # If time is missing or in the past, calculate the next Sunday 10 AM IST
        if not target_dt_utc or target_dt_utc < datetime.now(UTC_TZ):
            target_dt_utc = self.get_next_sunday_10am(datetime.now(UTC_TZ)) 
            config['next_run_dt'] = target_dt_utc
            self._save_config()

        now_utc = datetime.now(UTC_TZ) 
        remaining_time = target_dt_utc - now_utc
        
        # Ensure minimum 60s delay to let the bot fully initialize
        initial_delay = max(60, remaining_time.total_seconds()) 

        print(f"Calculated initial delay: {initial_delay / 3600:.2f} hours")

        # CRITICAL FIX: Stop the task cleanly if it's running, and then always call start().
        # This prevents the "unexpected keyword argument 'delay'" error upon restart/re-execution.
        if self.weekly_leaderboard_task.is_running():
            self.weekly_leaderboard_task.stop()
            print("Weekly leaderboard task stopped for clean restart.")
            
        # Start the task with the calculated delay. The interval (3600s) is in the decorator.
        # We also need a brief sleep to allow the stop event to fully process before starting a new thread.
        # Although technically synchronous, asyncio.sleep(0) often helps scheduling robustness, 
        # but since this is called from sync context, we rely on the next event loop tick.
        self.weekly_leaderboard_task.start(delay=initial_delay)


    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print('Bot is ready to generate leaderboards.')

    async def _run_leaderboard_logic(self, guild_id, config, interaction=None):
        """Core logic for calculating and sending the leaderboard."""
        guild = self.get_guild(guild_id)
        if not guild: return False

        channel = guild.get_channel(config['channel_id'])
        role = guild.get_role(config['role_id'])
        from_channel = guild.get_channel(config['from_channel_id'])
        top = config['top']

        if not all([channel, role, from_channel]): 
            print(f"Configuration missing for guild {guild_id}. Channel/Role/FromChannel not found.")
            return False
        if not guild.me.guild_permissions.manage_roles: 
            print(f"Bot lacks role management permissions in guild {guild_id}.")
            return False 

        # 1. Fetch Message History (Past 7 Days)
        seven_days_ago = datetime.now(UTC_TZ) - timedelta(days=7)
        message_counts = Counter()

        try:
            async for message in from_channel.history(after=seven_days_ago, limit=None):
                if not message.author.bot:
                    message_counts[message.author.id] += 1
        except Exception as e: 
            print(f"Error fetching message history: {e}")
            return False

        top_members_data = message_counts.most_common(top)
        
        # 2. Manage Roles
        current_role_holders = [member for member in role.members if member.guild.id == guild_id]

        for member in current_role_holders:
            # Check if the bot can manage the role (i.e., its own highest role is above the target role)
            if member.top_role.position < guild.me.top_role.position:
                try:
                    await member.remove_roles(role, reason="Weekly Leaderboard Reset")
                except discord.Forbidden:
                    print(f"Forbidden to remove role from {member.display_name}")
                except Exception as e:
                    print(f"Error removing role: {e}")

        top_users_objects = []
        for user_id, count in top_members_data:
            member = guild.get_member(user_id)
            if member:
                # Check if the bot can assign the role
                if member.top_role.position < guild.me.top_role.position:
                    try:
                        await member.add_roles(role, reason="Weekly Leaderboard Winner")
                    except discord.Forbidden:
                        print(f"Forbidden to add role to {member.display_name}")
                    except Exception as e:
                        print(f"Error adding role: {e}")
                
                top_users_objects.append((member, count))


        # 3. Construct Message
        def get_user_data(index):
            if index < len(top_users_objects):
                user, count = top_users_objects[index]
                return user.mention, count
            return "N/A", 0

        user1_mention, count1 = get_user_data(0)
        user2_mention, count2 = get_user_data(1)
        user3_mention, count3 = get_user_data(2)

        message_content = (
            f"Hello fellas, \n"
            f"We're back with the weekly leaderboard update!! <:Pika_Think:1444211873687011328>\n"
            f"Here are the top 3 active members past week–\n"
            f":first_place: Top 1: {user1_mention} with more than {count1} messages. \n"
            f"-# Gets 50k unb in cash\n"
            f":second_place: Top 2: {user2_mention} with more than {count2} messages.\n"
            f"-# Gets 25k unb in cash\n"
            f":third_place: Top 3: {user3_mention} with more than {count3} messages.\n"
            f"-# Gets 10k unb in cash\n\n"
            f"All of the top three members have been granted the role:\n"
            f"{role.mention}\n\n"
            f"Top 1 can change their server nickname once. Top 1 & 2 can have a custom role with name and colour based on their requests. Contact <@1193415556402008169> (<@&1405157360045002785>) within 24 hours to claim your awards."
        )

        # 4. Send the Leaderboard
        try:
            await channel.send(message_content)
        except discord.HTTPException as e:
            print(f"Error sending message to channel {channel.id}: {e}")
            return False

        # 5. Persistence: Calculate the NEXT Sunday 10 AM IST run time and save it.
        next_run = self.get_next_sunday_10am(datetime.now(UTC_TZ))
        self.leaderboard_config[guild_id]['next_run_dt'] = next_run
        self._save_config()

        print(f"Leaderboard ran successfully. Next run saved as: {next_run.astimezone(IST_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}")

        # Note: No need to restart the task here. The hourly loop handles the next check.
        return True

    # Set the interval (3600 seconds/1 hour) directly in the decorator.
    @tasks.loop(seconds=3600) 
    async def weekly_leaderboard_task(self):
        """The scheduled task that executes the leaderboard logic based on stored next run time."""
        await self.wait_until_ready()

        if not self.leaderboard_config:
            return

        now_utc = datetime.now(UTC_TZ)

        for guild_id, config in list(self.leaderboard_config.items()):
            target_dt_utc = config.get('next_run_dt')

            # Check if the current time is past the target time 
            if target_dt_utc and now_utc >= target_dt_utc:
                print(f"Scheduled run triggered for guild {guild_id}.")
                # If the run succeeds, it updates config['next_run_dt'] for the next week
                await self._run_leaderboard_logic(guild_id, config)

    @weekly_leaderboard_task.before_loop
    async def before_weekly_leaderboard_task(self):
        await asyncio.sleep(5)

bot = LeaderboardBot()

# --- SHARD ID COMMAND ---
@bot.tree.command(name="show-shard-id", description="Displays the shard ID for the current server.")
async def show_shard_id(interaction: discord.Interaction):
    if interaction.guild:
        shard_id = interaction.guild.shard_id
        await interaction.response.send_message(
            f"The shard ID for this server (`{interaction.guild.name}`) is: `{shard_id}`.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message("This command must be run in a guild.", ephemeral=True)
# ----------------------------


@bot.tree.command(name="setup-auto-leaderboard", description="Configure the bot to run the leaderboard automatically every Sunday at 10 AM IST")
@app_commands.describe(
    channel="The channel where the leaderboard message will be sent",
    role="The role to give to the top active members",
    top="How many members to fetch (e.g., 3)",
    from_channel="The channel to count messages from"
)
async def setup_auto_leaderboard(
    interaction: discord.Interaction, 
    channel: discord.TextChannel, 
    role: discord.Role, 
    top: int, 
    from_channel: discord.TextChannel
):
    # Permission checks
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("You need Administrator permissions to set up the leaderboard.", ephemeral=True)
    
    if top < 1 or top > 50:
        return await interaction.response.send_message("The 'top' value must be between 1 and 50.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild_id

    # 1. Calculate the initial next run time (Next Sunday 10 AM IST)
    next_run_dt_utc = bot.get_next_sunday_10am(datetime.now(UTC_TZ))

    # 2. Store Configuration in memory (saving the UTC datetime object)
    bot.leaderboard_config[guild_id] = {
        "channel_id": channel.id,
        "role_id": role.id,
        "top": top,
        "from_channel_id": from_channel.id,
        "next_run_dt": next_run_dt_utc 
    }

    # 3. Save Configuration to file (converts dt object to ISO string)
    bot._save_config()

    # 4. Restart the task with the calculated delay to ensure the timer starts now
    bot._start_weekly_task_with_persistence()

    # 5. Confirmation
    await interaction.followup.send(
        f"✅ Automated Weekly Leaderboard Setup Complete!\n"
        f"The leaderboard is now scheduled to run every **Sunday at 10:00 AM IST**.\n"
        f"The first scheduled run is on: {discord.utils.format_dt(next_run_dt_utc, 'F')} ({discord.utils.format_dt(next_run_dt_utc, 'R')}).",
        ephemeral=True
    )


@bot.tree.command(name="test-leaderboard", description="Immediately run the configured leaderboard logic.")
async def test_leaderboard(interaction: discord.Interaction):
    # Permission checks
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("You need Administrator permissions to run a test leaderboard.", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild_id

    if guild_id not in bot.leaderboard_config:
        return await interaction.followup.send("❌ Leaderboard has not been set up for this server. Please run `/setup-auto-leaderboard` first.", ephemeral=True)

    config = bot.leaderboard_config[guild_id]

    # Run the core logic, which updates the next run time and saves the config
    success = await bot._run_leaderboard_logic(guild_id, config, interaction)

    if success:
        channel = interaction.guild.get_channel(config['channel_id'])
        next_run_dt_utc = config['next_run_dt']
        await interaction.followup.send(
            f"✅ Test run complete. Leaderboard sent to {channel.mention} and roles managed.\n"
            f"The next *scheduled* run is now set for: {discord.utils.format_dt(next_run_dt_utc, 'F')}.", 
            ephemeral=True
        )
    else:
        await interaction.followup.send("❌ Test run failed. Check bot permissions and configuration.", ephemeral=True)


@bot.tree.command(name="leaderboard-timer", description="Shows the remaining time until the next automatic leaderboard run.")
async def leaderboard_timer(interaction: discord.Interaction):
    guild_id = interaction.guild_id

    if guild_id not in bot.leaderboard_config:
        return await interaction.response.send_message("❌ The automatic leaderboard has not been set up yet. Use `/setup-auto-leaderboard`.", ephemeral=True)

    config = bot.leaderboard_config[guild_id]
    next_run_dt_utc = config.get('next_run_dt')

    if not next_run_dt_utc:
        return await interaction.response.send_message("❌ Configuration error: Next run time is missing. Please run `/setup-auto-leaderboard` again.", ephemeral=True)

    now_utc = datetime.now(UTC_TZ)
    remaining_time = next_run_dt_utc - now_utc

    if remaining_time.total_seconds() < 0:
        timer_message = (
            f"🚨 The scheduled run was missed on {discord.utils.format_dt(next_run_dt_utc, 'F')} ({discord.utils.format_dt(next_run_dt_utc, 'R')}).\n"
            f"The task should run immediately on the next task cycle (within the hour). Use `/test-leaderboard` to force the run now."
        )
    else:
        timer_message = (
            f"⏳ The next automatic leaderboard update is scheduled for **Sunday at 10:00 AM IST**.\n"
            f"The exact time is: {discord.utils.format_dt(next_run_dt_utc, 'F')} ({discord.utils.format_dt(next_run_dt_utc, 'R')})."
        )

    await interaction.response.send_message(timer_message, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        print("Error: TOKEN not found. Please check your .env file.")
    else:
        try:
            bot.run(TOKEN)
        except discord.errors.LoginFailure:
            print("Error: Bot failed to log in. Check your DISCORD_TOKEN in the .env file.")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")