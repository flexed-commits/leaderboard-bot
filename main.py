import discord
from discord import app_commands
from discord.ext import commands
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

    async def setup_hook(self):
        # Sync slash commands with Discord
        await self.tree.sync()
        print("Slash commands synced!")

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print('Bot is ready to generate leaderboards.')

bot = LeaderboardBot()

@bot.tree.command(name="setup-auto-leaderboard", description="Generate a weekly leaderboard and assign roles")
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
    # 1. Acknowledge the command immediately because fetching history takes time
    await interaction.response.defer(ephemeral=True)

    try:
        # Check permissions
        if not interaction.guild.me.guild_permissions.manage_roles:
            await interaction.followup.send("❌ I do not have permission to Manage Roles.")
            return
        
        if role.position >= interaction.guild.me.top_role.position:
            await interaction.followup.send("❌ That role is higher than my highest role. I cannot manage it.")
            return

        # 2. Fetch Message History (Past 7 Days)
        seven_days_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)
        
        message_counts = Counter()
        
        # We assume the user wants to count valid user messages, excluding bots
        # Using limit=None to get ALL messages in the last 7 days. 
        # CAUTION: In very active channels, this might take a while.
        async for message in from_channel.history(after=seven_days_ago, limit=None):
            if not message.author.bot:
                message_counts[message.author.id] += 1

        # 3. Get Top N Users
        top_members_data = message_counts.most_common(top)
        
        if not top_members_data:
            await interaction.followup.send(f"❌ No messages found in {from_channel.mention} for the past 7 days.")
            return

        # 4. Manage Roles
        # First, remove the role from everyone who currently has it
        current_role_holders = role.members
        for member in current_role_holders:
            try:
                await member.remove_roles(role, reason="Weekly Leaderboard Reset")
            except discord.Forbidden:
                pass # Skip if we can't edit this specific member for some reason

        # Second, add role to the new winners
        top_users_objects = []
        
        for user_id, count in top_members_data:
            member = interaction.guild.get_member(user_id)
            if member:
                try:
                    await member.add_roles(role, reason="Weekly Leaderboard Winner")
                    top_users_objects.append((member, count))
                except discord.Forbidden:
                    top_users_objects.append((member, count)) # Add to list even if role failed
            else:
                # Member might have left the server
                continue

        # 5. Construct the Message
        # We need at least 1 user to format the message, but ideally 3 based on the prompt template.
        
        # Safe access helpers in case there are fewer than 3 active users
        def get_user_data(index):
            if index < len(top_users_objects):
                user, count = top_users_objects[index]
                return user.mention, count
            return "N/A", 0

        user1_mention, count1 = get_user_data(0)
        user2_mention, count2 = get_user_data(1)
        user3_mention, count3 = get_user_data(2)

        # Building the text strictly based on the user prompt
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

        # 7. Confirm to the command runner
        await interaction.followup.send(f"✅ Leaderboard generated successfully in {channel.mention}!")

    except Exception as e:
        await interaction.followup.send(f"❌ An error occurred: {str(e)}")
        print(e)

if __name__ == "__main__":
    if not TOKEN:
        print("Error: TOKEN not found. Please check your .env file.")
    else:
        bot.run(TOKEN)