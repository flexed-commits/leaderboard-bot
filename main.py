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
from zoneinfo import ZoneInfo # Used for named timezones (requires Python 3.9+)

# --- CONFIGURATION ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
CONFIG_FILE = 'leaderboard_data.json' # File to store persistent data

# Timezone and Target Time Setup (IST - India Standard Time)
IST_TZ = ZoneInfo("Asia/Kolkata")
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
                    return json.load(f)
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
                serializable_config[guild_id] = config.copy()
                # Ensure we don't save the datetime object itself, but the string representation
                if 'next_run_dt' in serializable_config[guild_id]:
                    serializable_config[guild_id]['next_run_dt'] = serializable_config[guild_id]['next_run_dt'].isoformat()
            
            json.dump(serializable_config, f, indent=4)
    
    def get_next_sunday_10am(self, start_time: datetime) -> datetime:
        """Calculates the date and time of the next Sunday at 10:00 AM IST."""
        
        # 1. Localize the start time to IST
        start_time_ist = start_time.astimezone(IST_TZ)
        
        # 2. Calculate days until next Sunday (0=Monday, 6=Sunday)
        days_ahead = TARGET_DAY - start_time_ist.weekday()
        if days_ahead <= 0:  # If today is Sunday or past Sunday, aim for next week
            days_ahead += 7

        # 3. Calculate the date of the next Sunday
        next_sunday = start_time_ist + timedelta(days=days_ahead)
        
        # 4. Set the time to 10:00 AM IST
        next_run_dt = next_sunday.replace(
            hour=TARGET_HOUR, 
            minute=TARGET_MINUTE, 
            second=0, 
            microsecond=0
        )
        
        # 5. Check if the calculated time is in the past (only possible if today is Sunday 
        #    and the calculation wrapped around incorrectly, or bot was offline).
        if next_run_dt < start_time_ist:
            next_run_dt += timedelta(days=7) # Go to the Sunday after that

        return next_run_dt.astimezone(datetime.timezone.utc) # Return as UTC for consistent internal use

    async def setup_hook(self):
        await self.tree.sync()
        print("Slash commands synced!")
        
        # Load and potentially convert the stored run time strings back to datetime objects
        for guild_id, config in self.leaderboard_config.items():
            if 'next_run_dt' in config and isinstance(config['next_run_dt'], str):
                # Convert stored ISO string back to timezone-aware datetime object
                config['next_run_dt'] = datetime.fromisoformat(config['next_run_dt']).astimezone(datetime.timezone.utc)

        # Start the timer task with the correct delay
        self._start_weekly_task_with_persistence()
        print("Weekly leaderboard task initialized/resumed.")

    def _start_weekly_task_with_persistence(self):
        """
        Calculates the time until the first configured guild's next scheduled run 
        and starts the task with that delay.
        """
        if not self.leaderboard_config:
            # Task will be started when setup is run for the first time
            if not self.weekly_leaderboard_task.is_running():
                 self.weekly_leaderboard_task.start(delay=86400) # Start with a day interval until setup
            return

        # We use the configuration of the first set up guild to determine the initial delay
        guild_id = next(iter(self.leaderboard_config))
        config = self.leaderboard_config[guild_id]
        
        target_dt_utc = config.get('next_run_dt')

        # If next_run_dt wasn't loaded (e.g., first run after update), set it to next Sunday
        if not target_dt_utc:
            target_dt_utc = self.get_next_sunday_10am(datetime.now(datetime.timezone.utc))
            config['next_run_dt'] = target_dt_utc
            self._save_config()

        now_utc = datetime.now(datetime.timezone.utc)
        remaining_time = target_dt_utc - now_utc
        initial_delay = max(0, remaining_time.total_seconds())
        
        print(f"Calculated initial delay: {initial_delay / 3600:.2f} hours")

        # Start the loop with the calculated initial delay
        # The loop interval (seconds=3600) is arbitrary since the delay handles the first run,
        # and subsequent runs are triggered by logic or restart. We use a short interval 
        # for frequent checks.
        if not self.weekly_leaderboard_task.is_running():
            self.weekly_leaderboard_task.start(seconds=3600, delay=initial_delay)
        else:
            self.weekly_leaderboard_task.restart(seconds=3600, delay=initial_delay)


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
        
        # --- Omitted Permissions/Error Checks for brevity, they remain the same ---
        if not all([channel, role, from_channel]): 
            # Send error via interaction if available
            return False 
        if not guild.me.guild_permissions.manage_roles: 
            return False 

        # 1. Fetch Message History (Past 7 Days)
        seven_days_ago = datetime.now(datetime.timezone.utc) - timedelta(days=7)
        message_counts = Counter()
        
        try:
            async for message in from_channel.history(after=seven_days_ago, limit=None):
                if not message.author.bot:
                    message_counts[message.author.id] += 1
        except Exception: 
            return False

        top_members_data = message_counts.most_common(top)
        if not top_members_data: 
            return False

        # 2. Manage Roles and Construct Message (Same logic)
        current_role_holders = [member for member in role.members if member.guild.id == guild_id]
        
        for member in current_role_holders:
            if member.top_role.position < guild.me.top_role.position:
                try:
                    await member.remove_roles(role, reason="Weekly Leaderboard Reset")
                except discord.Forbidden:
                    pass

        top_users_objects = []
        for user_id, count in top_members_data:
            member = guild.get_member(user_id)
            if member and member.top_role.position < guild.me.top_role.position:
                try:
                    await member.add_roles(role, reason="Weekly Leaderboard Winner")
                    top_users_objects.append((member, count))
                except discord.Forbidden:
                    top_users_objects.append((member, count))
            elif member:
                 top_users_objects.append((member, count))

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

        # 3. Send the Leaderboard
        await channel.send(message_content)

        # 4. Persistence: Calculate the NEXT Sunday 10 AM IST run time and save it.
        # This ensures the bot always targets the next weekly run time correctly.
        next_run = self.get_next_sunday_10am(datetime.now(datetime.timezone.utc))
        self.leaderboard_config[guild_id]['next_run_dt'] = next_run
        self._save_config()
        
        print(f"Leaderboard ran successfully. Next run saved as: {next_run.astimezone(IST_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}")

        # Restart the task with the *new* calculated delay to reset the timer
        self._start_weekly_task_with_persistence() 
        return True

    @tasks.loop(seconds=3600) # Runs every hour to check if the target time has passed
    async def weekly_leaderboard_task(self):
        """The scheduled task that executes the leaderboard logic based on stored next run time."""
        await self.wait_until_ready()
        
        if not self.leaderboard_config:
            return

        now_utc = datetime.now(datetime.timezone.utc)
        
        # Iterate over all configured guilds (though the current code only supports one guild setup)
        for guild_id, config in list(self.leaderboard_config.items()):
            target_dt_utc = config.get('next_run_dt')
            
            # Check if the current time is past the target time (with a 1-hour buffer)
            if target_dt_utc and now_utc >= target_dt_utc:
                # Run the logic, which will immediately reschedule the task for next week
                await self._run_leaderboard_logic(guild_id, config)


bot = LeaderboardBot()

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
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild_id

    # Validation checks... (omitted for brevity)

    # 1. Calculate the initial next run time (Next Sunday 10 AM IST)
    next_run_dt_utc = bot.get_next_sunday_10am(datetime.now(datetime.timezone.utc))
    next_run_dt_ist = next_run_dt_utc.astimezone(IST_TZ)

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
            f"The next run is now scheduled for the following Sunday at 10:00 AM IST: {discord.utils.format_dt(next_run_dt_utc, 'F')}.", 
            ephemeral=True
        )


@bot.tree.command(name="leaderboard-timer", description="Shows the remaining time until the next automatic leaderboard run.")
async def leaderboard_timer(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    
    if guild_id not in bot.leaderboard_config:
        return await interaction.response.send_message("❌ The automatic leaderboard has not been set up yet. Use `/setup-auto-leaderboard`.", ephemeral=True)

    config = bot.leaderboard_config[guild_id]
    next_run_dt_utc = config.get('next_run_dt')

    if not next_run_dt_utc:
        return await interaction.response.send_message("❌ Configuration error: Next run time is missing. Please run `/setup-auto-leaderboard` again.", ephemeral=True)

    now_utc = datetime.now(datetime.timezone.utc)
    remaining_time = next_run_dt_utc - now_utc

    if remaining_time.total_seconds() < 0:
        timer_message = (
            f"🚨 The scheduled run was missed on {discord.utils.format_dt(next_run_dt_utc, 'F')} ({discord.utils.format_dt(next_run_dt_utc, 'R')}).\n"
            f"The task should run immediately on the next task cycle (within the hour). Use `/test-leaderboard` to force the run now."
        )
    else:
        days = remaining_time.days
        hours, remainder = divmod(remaining_time.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)

        next_run_dt_ist = next_run_dt_utc.astimezone(IST_TZ)

        timer_message = (
            f"⏳ The next automatic leaderboard update is scheduled for **Sunday at 10:00 AM IST**.\n"
            f"Time remaining: **{days} days, {hours} hours, and {minutes} minutes.**\n"
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