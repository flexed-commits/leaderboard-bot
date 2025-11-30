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
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# ----------------------------------------------------
# DEBUGGING BLOCK START
# ----------------------------------------------------
if TOKEN:
    # Print only the first few characters to confirm it loaded, but keep the full token secure
    print(f"DEBUG: Token loaded successfully. Starts with: {TOKEN[:5]}...")
else:
    # This is the line that confirms the load_dotenv() failed
    print("FATAL DEBUG ERROR: DISCORD_TOKEN is None or empty after load_dotenv(). Check .env file.")
    exit(1) # Exit immediately so we don't proceed to login failure
# ----------------------------------------------------
# DEBUGGING BLOCK END
# ----------------------------------------------------

CONFIG_FILE = 'leaderboard_data.json' # File to store persistent data

# Timezone and Target Time Setup (IST - India Standard Time)
IST_TZ = pytz.timezone("Asia/Kolkata") # Changed to use pytz
UTC_TZ = pytz.utc                     # Defining UTC explicitly for clarity
TARGET_HOUR = 10  # 10 AM
TARGET_MINUTE = 0 # 0 minutes
TARGET_DAY = 6    # Sunday (Monday is 0, Sunday is 6)

# Initialize Bot with necessary intents
# ... (rest of the script remains unchanged)