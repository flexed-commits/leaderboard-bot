import discord
from discord import app_commands, Intents, Embed
from discord.ext import tasks
from datetime import datetime, timedelta, timezone
import json
import os
import asyncio

# --- Configuration File Path ---
CONFIG_FILE = 'leaderboard_config.json'
# Maximum number of messages to fetch from the source channel. 
# Discord's API is limited; fetching too many messages can be slow or fail.
MESSAGE_FETCH_LIMIT = 5000 
DEFAULT_APP_ID = "1444211873687011328" # Using the provided emoji ID as a placeholder for the bot's app ID if needed

# --- Persistence Functions ---
def load_config():
    """Loads the leaderboard configuration from a JSON file."""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                print("Error loading config. File is corrupted. Starting with default.")
                return {}
    return {}

def save_config(config):
    """Saves the leaderboard configuration to a JSON file."""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)

# --- Bot Implementation ---

class LeaderboardBot(discord.Client):
    def __init__(self):
        # Configure intents: We need Guilds, GuildMessages, and MessageContent (for counting)
        intents = Intents.default()
        intents.guilds = True
        intents.members = True # Required to assign roles and read all members
        intents.messages = True
        intents.message_content = True # Required to read message content for counting
        
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.config = load_config()
        self.is_ready = False # Flag to ensure configuration and tasks are set only once
        self.role_clear_message = None # To store the temporary role clear message

    async def on_ready(self):
        """Called when the bot is connected and ready."""
        if not self.is_ready:
            print(f'Logged in as {self.user} (ID: {self.user.id})')
            
            # Sync application commands
            await self.tree.sync()
            print("Commands synced.")

            # Start the persistent task loop
            if self.config:
                print("Configuration loaded. Starting weekly task checker.")
                self.weekly_leaderboard_check.start()
            else:
                print("No configuration found. Awaiting /setup-auto-leaderboard.")
                
            self.is_ready = True

    @tasks.loop(minutes=1)
    async def weekly_leaderboard_check(self):
        """
        Checks every minute if the current time matches the scheduled time (Sun 4:30 AM GMT).
        This method ensures the schedule is maintained even if the bot restarts.
        """
        now_utc = datetime.now(timezone.utc)
        
        # Check for Sunday (weekday 6) at 4:30 AM GMT
        if now_utc.weekday() == 6 and now_utc.hour == 4 and now_utc.minute == 30:
            print(f"[{now_utc.isoformat()}] Scheduled run triggered.")
            await self.calculate_and_send_leaderboard()
        
    @weekly_leaderboard_check.before_loop
    async def before_weekly_leaderboard_check(self):
        """Waits for the bot to connect before starting the task loop."""
        await self.wait_until_ready()
        print("Task loop startup complete.")

    def get_last_sunday_430am_utc(self):
        """Calculates the timestamp for the start of the current week's leaderboard period (Last Sunday 4:30 AM UTC)."""
        now_utc = datetime.now(timezone.utc)
        
        # Calculate the datetime for the last Sunday at 4:30 AM UTC
        days_since_sunday = now_utc.weekday() - 6  # Monday=0, Sunday=6
        if days_since_sunday < 0:
            days_since_sunday = 6
            
        last_sunday = now_utc - timedelta(days=days_since_sunday)
        
        # Set the time to 4:30 AM
        start_time = last_sunday.replace(hour=4, minute=30, second=0, microsecond=0)

        # If the start_time calculated is actually *future* (i.e., we are between Sun 00:00 and 04:30), 
        # we need to look back to the previous Sunday.
        if start_time > now_utc:
             start_time = start_time - timedelta(days=7)
             
        return start_time

    def get_next_sunday_430am_utc(self):
        """Calculates the exact datetime for the next Sunday at 4:30 AM UTC."""
        now_utc = datetime.now(timezone.utc)
        
        # Calculate the next Sunday
        days_to_sunday = (6 - now_utc.weekday() + 7) % 7
        next_sunday_date = now_utc + timedelta(days=days_to_sunday)
        
        # Set the time to 4:30 AM
        next_run = next_sunday_date.replace(hour=4, minute=30, second=0, microsecond=0)
        
        # If the calculated time is already in the past, move to the Sunday after next
        if next_run < now_utc:
            next_run = next_run + timedelta(days=7)

        return next_run

    async def fetch_message_counts(self, guild_id, from_channel_id, start_time):
        """Fetches messages and counts user activity."""
        guild = self.get_guild(guild_id)
        if not guild:
            return None, "Guild not found."

        from_channel = guild.get_channel(from_channel_id)
        if not from_channel or not isinstance(from_channel, (discord.TextChannel, discord.ForumChannel)):
            return None, f"Source channel with ID {from_channel_id} not found or is not a text/forum channel in guild {guild_id}."

        message_counts = {}
        
        try:
            print(f"Fetching messages in {from_channel.name} since {start_time.isoformat()}")
            
            # Fetch messages from the specified time, up to the global limit
            async for message in from_channel.history(limit=MESSAGE_FETCH_LIMIT, after=start_time):
                # Ignore bot messages
                if message.author.bot:
                    continue

                user_id = message.author.id
                message_counts[user_id] = message_counts.get(user_id, 0) + 1
                
            print(f"Finished fetching and counting messages. Total unique users: {len(message_counts)}")

            # Sort users by message count (descending)
            sorted_users = sorted(message_counts.items(), key=lambda item: item[1], reverse=True)
            return sorted_users, None

        except discord.errors.Forbidden:
            return None, f"I do not have permission to read message history in channel {from_channel.mention}."
        except Exception as e:
            print(f"An error occurred during message fetching: {e}")
            return None, f"An unexpected error occurred while fetching messages: {e}"

    async def handle_role_assignment(self, guild, top_users, role_id):
        """Clears role from all members and assigns it to top users."""
        role = guild.get_role(role_id)
        if not role:
            return f"Role with ID {role_id} not found in this server."

        # 1. Clear role from all members
        members_with_role = [m for m in guild.members if role in m.roles]
        
        # Send a temporary message about role clearance
        target_channel = guild.get_channel(self.config['channel_id'])
        if target_channel and isinstance(target_channel, discord.TextChannel):
            self.role_clear_message = await target_channel.send(
                f"Preparing the leaderboard... Clearing the **{role.name}** role from **{len(members_with_role)}** members now. Please wait."
            )

        print(f"Clearing role {role.name} from {len(members_with_role)} members...")
        clear_tasks = []
        for member in members_with_role:
            clear_tasks.append(member.remove_roles(role, reason="Weekly Leaderboard Role Reset"))
        
        # Execute role removal tasks concurrently
        await asyncio.gather(*clear_tasks, return_exceptions=True)
        print("Role clearance complete.")

        # 2. Assign role to top users
        top_user_ids = [user_id for user_id, count in top_users]
        assign_tasks = []
        
        for user_id in top_user_ids:
            member = guild.get_member(user_id)
            if member and role not in member.roles:
                assign_tasks.append(member.add_roles(role, reason="Weekly Leaderboard Top Member Award"))

        # Execute role assignment tasks concurrently
        await asyncio.gather(*assign_tasks, return_exceptions=True)
        print("Role assignment to top users complete.")
        
        return None

    def create_leaderboard_message(self, top_users, role_name, top_n):
        """Constructs the final message content."""
        emoji = "<:Pika_Think:1444211873687011328>" # Hardcoded emoji ID
        contact_user_id = "1193415556402008169"    # Hardcoded contact user ID
        contact_role_id = "1405157360045002785"    # Hardcoded contact role ID
        
        message_parts = [
            f"Hello fellas, \nWe're back with the weekly leaderboard update!! {emoji}\n"
        ]
        
        # Top N Users List
        message_parts.append(f"Here are the top {top_n} active members past week–")

        ranks = [
            (":first_place: Top 1", "50k unb in cash"),
            (":second_place: Top 2", "25k unb in cash"),
            (":third_place: Top 3", "10k unb in cash"),
        ]
        
        for i in range(min(top_n, len(top_users))):
            rank_text, reward = ranks[i] if i < len(ranks) else (f"Rank {i+1}", "No set reward.")
            user_id, count = top_users[i]
            
            # Use 'x' placeholder if user is not found or for formatting consistency
            user_mention = f"<@{user_id}>" if self.get_user(user_id) else f"user{i+1}"
            
            message_parts.append(
                f"{rank_text}: {user_mention} with more than {count} messages. \n"
                f"-# Gets {reward}"
            )

        if not top_users:
            message_parts.append("*(No active members found this week.)*")

        # Footer and Contact
        message_parts.append(f"\nAll of the top {min(top_n, len(top_users))} members have been granted the role:\n**{role_name}**")
        
        if top_n >= 1:
            message_parts.append(
                "\nTop 1 can change their server nickname once. Top 1 & 2 can have a custom role with name and colour based on their requests. "
                f"Contact <@{contact_user_id}> (<@&{contact_role_id}>) within 24 hours to claim your awards."
            )
        
        return "\n".join(message_parts)

    async def calculate_and_send_leaderboard(self):
        """The core logic for fetching data, assigning roles, and sending the message."""
        if not self.config:
            print("Leaderboard task skipped: Configuration is missing.")
            return

        guild_id = self.config['guild_id']
        channel_id = self.config['channel_id']
        from_channel_id = self.config['from_channel_id']
        role_id = self.config['role_id']
        top_n = self.config['top_n']

        guild = self.get_guild(guild_id)
        channel = self.get_channel(channel_id)
        role = guild.get_role(role_id) if guild else None

        if not guild or not channel or not role:
            print(f"Skipping run: Guild ({guild_id}), Channel ({channel_id}), or Role ({role_id}) not found.")
            return

        start_time = self.get_last_sunday_430am_utc()

        # 1. Fetch Message Counts
        top_users, error = await self.fetch_message_counts(guild_id, from_channel_id, start_time)

        if error:
            await channel.send(f":x: **Leaderboard Generation Error:** {error}")
            return
            
        # 2. Handle Role Assignment
        # Only consider the top N users for role assignment
        top_n_users = top_users[:top_n]
        
        role_error = await self.handle_role_assignment(guild, top_n_users, role_id)

        if role_error:
            await channel.send(f":warning: **Role Assignment Warning:** {role_error}")
            
        # Delete the temporary role clear message if it was sent
        if self.role_clear_message:
            await self.role_clear_message.delete()
            self.role_clear_message = None

        # 3. Create and Send Final Message
        final_message = self.create_leaderboard_message(top_n_users, role.name, top_n)
        await channel.send(final_message)
        print(f"Leaderboard message sent to {channel.name}.")


    # --- Application Commands ---
    
    @app_commands.command(name="setup-auto-leaderboard", description="Set up the automated weekly server activity leaderboard.")
    @app_commands.describe(
        channel="The channel where the final leaderboard message will be sent.",
        role="The role to be given to the top members (will be cleared first).",
        top="The number of top users to fetch and reward (e.g., 3).",
        from_channel="The channel to count messages from for the leaderboard data."
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def setup_auto_leaderboard(self, interaction: discord.Interaction, 
                                     channel: discord.TextChannel, 
                                     role: discord.Role, 
                                     top: app_commands.Range[int, 1, 10], # Limit top N to 1-10
                                     from_channel: discord.TextChannel):
        
        await interaction.response.defer(thinking=True, ephemeral=True)
        
        # Save configuration
        self.config = {
            'guild_id': interaction.guild_id,
            'channel_id': channel.id,
            'role_id': role.id,
            'top_n': top,
            'from_channel_id': from_channel.id,
        }
        save_config(self.config)

        # Start or restart the task loop
        if self.weekly_leaderboard_check.is_running():
            self.weekly_leaderboard_check.restart()
        else:
            self.weekly_leaderboard_check.start()
            
        # Provide feedback
        embed = Embed(
            title="✅ Leaderboard Setup Complete",
            description=(
                f"The weekly leaderboard has been scheduled for every **Sunday at 4:30 AM GMT**.\n\n"
                f"**Output Channel:** {channel.mention}\n"
                f"**Source Channel (Activity):** {from_channel.mention}\n"
                f"**Role to Assign:** {role.mention}\n"
                f"**Top Users Count:** {top}"
            ),
            color=discord.Color.green()
        )
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    @setup_auto_leaderboard.error
    async def setup_auto_leaderboard_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ You must be an administrator to set up the leaderboard.", ephemeral=True)
        else:
            await interaction.response.send_message(f"An error occurred during setup: {error}", ephemeral=True)


    @app_commands.command(name="test-leaderboard", description="Manually trigger the leaderboard calculation and send the message.")
    @app_commands.checks.has_permissions(administrator=True)
    async def test_leaderboard(self, interaction: discord.Interaction):
        
        if not self.config:
            await interaction.response.send_message(
                "❌ Leaderboard is not configured. Please run `/setup-auto-leaderboard` first.", ephemeral=True
            )
            return

        await interaction.response.send_message("⚙️ Manually calculating and sending the leaderboard now...", ephemeral=True)
        
        # Run the core logic
        await self.calculate_and_send_leaderboard()
        
        await interaction.edit_original_response(content="✅ Leaderboard successfully generated and sent to the configured channel.")
        
    @test_leaderboard.error
    async def test_leaderboard_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("❌ You must be an administrator to test the leaderboard.", ephemeral=True)
        else:
            await interaction.response.send_message(f"An error occurred during the test run: {error}", ephemeral=True)


    @app_commands.command(name="leaderboard-timer", description="Shows the time remaining until the next scheduled leaderboard update.")
    async def leaderboard_timer(self, interaction: discord.Interaction):
        
        if not self.config:
            await interaction.response.send_message(
                "❌ Leaderboard is not configured. Please run `/setup-auto-leaderboard` first.", ephemeral=True
            )
            return
            
        await interaction.response.defer(thinking=True)

        next_run = self.get_next_sunday_430am_utc()
        now_utc = datetime.now(timezone.utc)
        
        time_until_next = next_run - now_utc

        days = time_until_next.days
        hours = time_until_next.seconds // 3600
        minutes = (time_until_next.seconds % 3600) // 60
        seconds = time_until_next.seconds % 60
        
        # Format the countdown string
        countdown_parts = []
        if days > 0: countdown_parts.append(f"{days} day{'s' if days != 1 else ''}")
        if hours > 0: countdown_parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes > 0: countdown_parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
        # Only show seconds if the total time is very short
        if days == 0 and hours == 0 and minutes < 5: countdown_parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")

        countdown = ", ".join(countdown_parts) if countdown_parts else "Less than a minute"

        embed = Embed(
            title="⏰ Next Leaderboard Update",
            description=(
                f"The next leaderboard update is scheduled for:\n"
                f"**{next_run.strftime('%A, %B %d')} at 04:30 AM GMT**\n\n"
                f"**Time Remaining:** {countdown}"
            ),
            color=discord.Color.blue()
        )
        
        await interaction.followup.send(embed=embed)


# --- Bot Run ---
# The environment must have a DISCORD_TOKEN set.
if __name__ == "__main__":
    # Enable debugging logs for Firestore/Firebase in case it was used, though we are using JSON here
    # setLogLevel('Debug') 
    
    # Get token from environment variable
    TOKEN = os.getenv('DISCORD_TOKEN')
    if not TOKEN:
        print("FATAL ERROR: DISCORD_TOKEN environment variable not set.")
    else:
        bot = LeaderboardBot()
        try:
            bot.run(TOKEN)
        except discord.LoginFailure:
            print("FATAL ERROR: Failed to log in. Check your DISCORD_TOKEN.")
        except KeyboardInterrupt:
            print("Bot shutting down...")
            if bot.weekly_leaderboard_check.is_running():
                bot.weekly_leaderboard_check.cancel()
            exit()