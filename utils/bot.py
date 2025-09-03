import os
import discord
from discord import Object
from discord.ext import commands
from dotenv import load_dotenv
import logging
import time



load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.messages = True
intents.members = True
intents.message_content = True
intents.guilds = True
intents.reactions = True
intents.emojis = True
intents.guild_messages = True

bot = commands.Bot(
    command_prefix=".",
    intents=intents,
    help_command=None,
    shard_id=0,
    shard_count=1
)

WELCOME_CHANNEL_ID = 1371686628510269460


# Add the Discord handler
DISCORD_CHANNEL_ID = 1372837110125694986  # Your channel ID here
TIMEZONE_NAME = "America/Chicago"  # Replace with your preferred timezone (e.g., 'UTC', 'Europe/Berlin', etc.)
GUILD_ID2 = 1265120128295632926
guild_object = discord.Object(id=1265120128295632926)
# Minimum similarity score to consider a match
SIMILARITY_THRESHOLD = 80
s = " " * 10
# Expose the handler if needed in codex.py
__all__ = [
        "bot", "TOKEN", "guild_object",
        "GUILD_ID2", "WELCOME_CHANNEL_ID",
        "SIMILARITY_THRESHOLD", "s"
]
