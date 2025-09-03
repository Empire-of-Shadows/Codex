import logging
from discord.ext import commands
from fuzzywuzzy import fuzz

from utils.bot import SIMILARITY_THRESHOLD
from Guide.guide import guide_manager  # Import the guide_manager instance
from Guide.guide_words import KEYWORD_MAP, HELP_WORDS

from utils.logger import get_logger

logger = get_logger("GuideMentionListener")

class HelpListener(commands.Cog):
	def __init__(self, bot):
		self.bot = bot

	@commands.Cog.listener()
	async def on_message(self, message):
		# Add debug logging
		logger.info(f"Message received: {message.content}")
		logger.info(f"Author: {message.author} (bot: {message.author.bot})")
		logger.info(f"Mentions: {[user.id for user in message.mentions]}")
		logger.info(f"Bot user ID: {self.bot.user.id if self.bot.user else 'None'}")

		# Allow messages from selected bots by ID only
		allowed_bot_ids = [1324702268666417192, 1324623083646095453]

		if message.author.bot and message.author.id not in allowed_bot_ids:
			logger.info("Message is from a non-allowed bot, ignoring")
			return

		if self.bot.user not in message.mentions:
			logger.info("Bot not mentioned in message")
			return

		logger.info("Bot was mentioned! Processing message...")

		content = message.content.lower().strip()
		logger.info(f"Processing content: '{content}'")

		# Who are we helping?
		if message.author.bot:
			mentioned_users = [m for m in message.mentions if m.id != self.bot.user.id]
			if not mentioned_users:
				return
			author_id = mentioned_users[0].id
		else:
			author_id = message.author.id

		# Exact match
		for keyword, menu_name in sorted(KEYWORD_MAP.items(), key=lambda x: len(x[0]), reverse=True):
			if keyword in content:
				embed, view = await guide_manager.get_embed_for_selection(menu_name, author_id=author_id)
				logger.info(f"Exact match: {keyword} in {content}")
				await message.channel.send(embed=embed, view=view)
				return

		# Fuzzy match
		for keyword, menu_name in sorted(KEYWORD_MAP.items(), key=lambda x: len(x[0]), reverse=True):
			if len(keyword) < 4:
				continue
			similarity = fuzz.token_set_ratio(keyword, content)
			if similarity >= SIMILARITY_THRESHOLD:
				embed, view = await guide_manager.get_embed_for_selection(menu_name, author_id=author_id)
				logger.info(f"Fuzzy match: {keyword} ~ {content} ({similarity}%)")
				await message.channel.send(embed=embed, view=view)
				return

		# Generic help
		if any(word in content for word in HELP_WORDS):
			logger.info("Generic help triggered")
			embed, view = await guide_manager.get_help_menu(author_id=author_id)
			await message.channel.send(embed=embed, view=view)
			return

		logger.info("No matches found for the message")


async def setup(bot):
	await guide_manager.initialize_database()
	await bot.add_cog(HelpListener(bot))