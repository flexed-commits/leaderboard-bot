import discord
from discord import app_commands
from discord.ext import commands, tasks
import datetime
from collections import Counter
import asyncio
import os
from dotenv import load_dotenv

# --- CONFIGURATION ---
# Load environment variables from .env file
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# Initialize Bot with necessary intents
intents = discord.Intents.default()
intents.members = True          # Required for Role management
intents.message_content = True  # Required to read message history
intents.guilds = True

class LeaderboardBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        # Dictionary to store guild configuration {guild_id: {channel_id, role_id, top, from_channel_id}}
        # NOTE: This data is NOT persistent and will be lost on bot restart.
        self.leaderboard_config = {}
        # REMOVED: self.weekly_leaderboard_task.start()
        # It will be started in setup_hook()

    async def setup_hook(self):
        # Sync slash commands with Discord
        await self.tree.sync()
        print("Slash commands synced!")
        
        # FIX: Start the task here, inside an async method, after the bot object is created.
        self.weekly_leaderboard_task.start() 
        print("Weekly leaderboard task started!")

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print('Bot is ready to generate leaderboards.')

    async def _run_leaderboard_logic(self, guild_id, config, interaction=None):
        """Core logic for calculating and sending the leaderboard.
        
        Args:
            guild_id (int): The ID of the guild to run the logic for.
            config (dict): The stored configuration for the guild.
            interaction (discord.Interaction, optional): The interaction object if run via command.
        
        Returns:
            bool: True if successful, False otherwise.
        """
        guild = self.get_guild(guild_id)
        if not guild:
            print(f"Guild {guild_id} not found.")
            return False

        # Fetch required objects
        channel = guild.get_channel(config['channel_id'])
        role = guild.get_role(config['role_id'])
        from_channel = guild.get_channel(config['from_channel_id'])
        top = config['top']

        if not all([channel, role, from_channel]):
            error_msg = f"Error: One or more required channels/roles not found in guild {guild_id}. Please run setup again."
            if interaction:
                await interaction.followup.send(f"❌ {error_msg}")
            print(error_msg)
            return False

        # 1. Check Bot Permissions
        if not guild.me.guild_permissions.manage_roles:
            error_msg = f"Bot lacks 'Manage Roles' permission in guild {guild_id}"
            if interaction:
                await interaction.followup.send(f"❌ {error_msg}")
            print(error_msg)
            return False
        
        # 2. Fetch Message History (Past 7 Days)
        seven_days_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)
        message_counts = Counter()
        
        try:
            # We use limit=None to fetch all messages in the time frame
            async for message in from_channel.history(after=seven_days_ago, limit=None):
                if not message.author.bot:
                    message_counts[message.author.id] += 1
        except discord.Forbidden:
            error_msg = f"Bot lacks read history permission in {from_channel.mention}"
            if interaction:
                await interaction.followup.send(f"❌ {error_msg}")
            print(error_msg)
            return False
        except discord.HTTPException as e:
            error_msg = f"HTTP error fetching messages: {e}"
            if interaction:
                await interaction.followup.send(f"❌ {error_msg}")
            print(error_msg)
            return False

        # 3. Get Top N Users
        top_members_data = message_counts.most_common(top)
        
        if not top_members_data:
            info_msg = f"No messages found in {from_channel.mention} for the past 7 days."
            if interaction:
                await interaction.followup.send(f"ℹ️ {info_msg} Leaderboard skipped.")
            print(info_msg)
            return False

        # 4. Manage Roles
        current_role_holders = [member for member in role.members if member.guild.id == guild_id]
        
        # Remove role from all current holders
        for member in current_role_holders:
            # Check if bot can manage the member's roles
            if member.top_role.position < guild.me.top_role.position:
                try:
                    await member.remove_roles(role, reason="Weekly Leaderboard Reset")
                except discord.Forbidden:
                    pass

        # Add role to the new winners
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

        # 5. Construct the Message
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

        # 6. Send the Leaderboard
        await channel.send(message_content)
        return True

    @tasks.loop(hours=168.0) # Runs every 7 days (168 hours)
    async def weekly_leaderboard_task(self):
        """The scheduled task that executes the leaderboard logic for all configured guilds."""
        await self.wait_until_ready() # Wait until the bot is connected and ready
        
        if not self.leaderboard_config:
            return

        for guild_id, config in self.leaderboard_config.items():
            await self._run_leaderboard_logic(guild_id, config)

    @weekly_leaderboard_task.before_loop
    async def before_weekly_leaderboard_task(self):
        # Optional: Wait a few seconds to ensure the client is fully ready
        await asyncio.sleep(5)

bot = LeaderboardBot()

@bot.tree.command(name="setup-auto-leaderboard", description="Configure the bot to run the leaderboard automatically every 7 days")
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
    # Standard validation checks
    if not interaction.guild.me.guild_permissions.manage_roles:
        return await interaction.response.send_message("❌ I do not have permission to Manage Roles.", ephemeral=True)
    
    if role.position >= interaction.guild.me.top_role.position:
        return await interaction.response.send_message("❌ That role is higher than my highest role. I cannot manage it.", ephemeral=True)

    # Store Configuration
    bot.leaderboard_config[interaction.guild.id] = {
        "channel_id": channel.id,
        "role_id": role.id,
        "top": top,
        "from_channel_id": from_channel.id
    }

    # Confirmation
    await interaction.response.send_message(
        f"✅ Automated Weekly Leaderboard Setup Complete!\n"
        f"The leaderboard will run automatically every **7 days (168 hours)**, sending the results to {channel.mention} and managing the role {role.mention}.\n\n"
        f"**WARNING:** This configuration is stored in the bot's memory and will be **lost if the bot restarts**.",
        ephemeral=True
    )


@bot.tree.command(name="test-leaderboard", description="Immediately run the configured leaderboard logic.")
async def test_leaderboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    guild_id = interaction.guild_id
    if guild_id not in bot.leaderboard_config:
        return await interaction.followup.send("❌ Leaderboard has not been set up for this server. Please run `/setup-auto-leaderboard` first.", ephemeral=True)

    config = bot.leaderboard_config[guild_id]
    
    # Run the core logic
    success = await bot._run_leaderboard_logic(guild_id, config, interaction)

    if success:
        channel = interaction.guild.get_channel(config['channel_id'])
        await interaction.followup.send(f"✅ Test run complete. Leaderboard sent to {channel.mention} and roles managed.", ephemeral=True)


@bot.tree.command(name="leaderboard-timer", description="Shows the remaining time until the next automatic leaderboard run.")
async def leaderboard_timer(interaction: discord.Interaction):
    # Check if the task is running and configured
    if not bot.weekly_leaderboard_task.is_running() or interaction.guild_id not in bot.leaderboard_config:
        return await interaction.response.send_message("❌ The automatic leaderboard is not running or has not been set up yet. Use `/setup-auto-leaderboard`.", ephemeral=True)

    # Get the scheduled time for the next run
    next_run = bot.weekly_leaderboard_task.next_iteration
    
    # next_iteration returns a naive datetime object if running locally, 
    # but since we use datetime.datetime.now(datetime.timezone.utc) in the loop logic, 
    # we should use a timezone-aware comparison here.
    now = datetime.datetime.now(datetime.timezone.utc)
    
    if not next_run:
         # Should not happen if the task is running, but as a fallback
        return await interaction.response.send_message("ℹ️ The weekly task is running, but the next iteration time could not be calculated yet.", ephemeral=True)

    # Calculate remaining time
    remaining_time = next_run - now

    days = remaining_time.days
    hours, remainder = divmod(remaining_time.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    timer_message = (
        f"⏳ The next automatic leaderboard update is scheduled to run in:\n"
        f"**{days} days, {hours} hours, and {minutes} minutes.**\n"
        f"The run is scheduled for {discord.utils.format_dt(next_run, 'F')} ({discord.utils.format_dt(next_run, 'R')})."
    )
    
    await interaction.response.send_message(timer_message, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        print("Error: TOKEN not found. Please check your .env file.")
    else:
        bot.run(TOKEN)