import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Dict
from collections import defaultdict
import aiohttp
import discord
from utils.bot import bot, WELCOME_CHANNEL_ID, TOKEN, s
from utils.cache import cache_manager
from Guide.guide import get_help_menu
from utils.logger import get_logger

logger = get_logger("Joining")

# Rate limiting storage - in production, consider using Redis or database
dm_rate_limits: Dict[int, Dict[str, Any]] = defaultdict(lambda: {
	'count': 0,
	'last_reset': datetime.now(timezone.utc),
	'blocked_until': None
})

# Rate limit configuration
DM_RATE_LIMITS = {
	'new_account_days': 30,  # Accounts newer than 30 days are considered "new"
	'max_dms_per_hour': 2,  # Max DMs per hour for new accounts
	'max_dms_per_day': 5,  # Max DMs per day for new accounts
	'block_duration_hours': 24,  # Block duration when limit exceeded
}


async def can_send_dm(member: discord.Member) -> tuple[bool, str]:
	"""
    Check if we can send a DM to this member based on rate limits.
    Returns (can_send, reason)
    """
	now = datetime.now(timezone.utc)
	account_age = now - member.created_at

	# Only apply rate limits to new accounts
	if account_age.days >= DM_RATE_LIMITS['new_account_days']:
		return True, "Account old enough"

	user_limits = dm_rate_limits[member.id]

	# Check if user is currently blocked
	if user_limits.get('blocked_until') and now < user_limits['blocked_until']:
		remaining = user_limits['blocked_until'] - now
		return False, f"Blocked for {remaining.seconds // 3600}h {(remaining.seconds % 3600) // 60}m"

	# Reset counters if needed (hourly reset)
	if now - user_limits['last_reset'] >= timedelta(hours=1):
		user_limits['count'] = 0
		user_limits['last_reset'] = now
		user_limits['blocked_until'] = None

	# Check daily limit (simplified - you might want more sophisticated tracking)
	if user_limits['count'] >= DM_RATE_LIMITS['max_dms_per_hour']:
		# Block the user
		user_limits['blocked_until'] = now + timedelta(hours=DM_RATE_LIMITS['block_duration_hours'])
		logger.warning(
			f"User {member} ({member.id}) hit DM rate limit, blocked for {DM_RATE_LIMITS['block_duration_hours']} hours")
		return False, "Rate limit exceeded"

	return True, "Within limits"


async def record_dm_sent(member: discord.Member):
	"""Record that a DM was sent to this member"""
	user_limits = dm_rate_limits[member.id]
	user_limits['count'] += 1
	logger.info(f"DM count for {member} ({member.id}): {user_limits['count']}")


@bot.event
async def on_member_join(member):
	"""
    Triggered when a user joins the server. Implements account age restrictions
    and rate-limited DM notifications.
    """
	logger.info(f"\n{s}New member joined: {member} ({member.id})\n")

	now = datetime.now(timezone.utc)
	account_age = now - member.created_at
	guild = member.guild

	# Default avatar fallback
	avatar_url = member.display_avatar.url if member.display_avatar else "https://cdn.discordapp.com/embed/avatars/0.png"

	if account_age.days < 60:
		# Account is too new — check if we can send DM with rate limiting
		can_dm, reason = await can_send_dm(member)

		if can_dm:
			try:
				await member.send(
					f"Hey {member.name}! 👋\n\n"
					f"Unfortunately, your Discord account is too new to join our server (created {account_age.days} days ago).\n"
					f"We require accounts to be at least 60 days old to help prevent spam and protect our community.\n\n"
					f"You're welcome to try again once your account is older. Thanks for understanding! 🙏"
				)
				await record_dm_sent(member)
				logger.info(f"\n{s}Sent DM to {member} about new account restriction.\n")
			except discord.Forbidden:
				logger.warning(f"\n{s}Could not DM {member} before kick (Forbidden).\n")
			except Exception as e:
				logger.error(f"\n{s}Failed to DM {member}: {e}")
		else:
			logger.info(f"\n{s}Skipped DM to {member} due to rate limiting: {reason}\n")

		try:
			await asyncio.sleep(1.2)
			await member.kick(reason=f"Account too new ({account_age.days} days old)")
			logger.info(f"\n{s}Kicked {member} due to account age ({account_age.days} days).\n")
		except Exception as e:
			logger.error(f"\n{s}Failed to kick {member}: {e}\n")

		await asyncio.sleep(1.2)  # Sleep after kick to pace
		return

	# Account is old enough - proceed with welcome
	channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
	if channel:
		try:
			# Update members cache for this guild
			await cache_manager.cache_members(member.guild)
			logger.info(f"\n{s}Member cache updated for {member.guild.name}\n")
		except Exception as e:
			logger.error(f"\n{s}Error updating member cache for {member.guild.name}: {e}\n")

		try:
			await asyncio.sleep(1.2)
			await send_welcome_message(member)
			logger.info(f"Interactive welcome message sent for {member}\n")
		except Exception as e:
			logger.error(f"Error sending welcome message: {e}\n")


# Rest of your existing functions remain the same...
async def send_welcome_message(member: discord.Member, avatar_url: str = None):
	# Your existing send_welcome_message function stays the same
	url = f"https://discord.com/api/v10/channels/{WELCOME_CHANNEL_ID}/messages"
	if avatar_url is None:
		avatar_url = member.display_avatar.url if member.display_avatar else "https://cdn.discordapp.com/embed/avatars/0.png"
	headers = {
		'Authorization': f"Bot {TOKEN}",
		'Content-Type': 'application/json'
	}

	json_payload = {
		"flags": 1 << 15,  # suppress embeds, required for component messages
		"components": [
			{
				"type": 17,  # Container
				"accent_color": 0x5865F2,  # Optional accent
				"components": [
					{
						"type": 12,  # Media Gallery
						"items": [
							{
								"media": {
									"url": avatar_url,
									"description": "Your avatar"
								}
							}
						]
					},
					{
						"type": 10,  # Text Display
						"content": f"# Welcome to the server, <@{member.id}>!"
					},
					{
						"type": 1,  # Action row with buttons
						"components": [
							{
								"type": 2,  # Button
								"label": "✅ Guide",
								"style": 3,  # Green
								"custom_id": "Need Help?"
							},
							{
								"type": 2,  # Button
								"label": "📜 Rules",
								"style": 5,  # Link
								"url": "https://discord.com/channels/1265120128295632926/1265122523599863930"
							},
							{
								"type": 2,
								"label": "🗣️Come Chat!",
								"style": 5,
								"url": "https://discord.com/channels/1265120128295632926/1265122926823211018"
							}
						]
					},
					{
						"type": 14,
						"divider": True,
						"spacing": 2
					},
					{
						"type": 10,
						"content": (":wave: Welcome to the Discord server!\n"
									"We are a community of gamers and love that you made it here.\n"
									"Feedback is welcome and helpful.\n"
									"Use `/suggest` to start your suggestion in #suggestions.\n"
									"Don't want your name on it? Use the anonymous option!\n")
					},
					{
						"type": 14,  # Separator
						"divider": True,
						"spacing": 1
					},
					{
						"type": 10,
						"content": "🎮 Some other channels you might like\n"
					},
					{
						"type": 1,
						"components": [
							{
								"type": 2,
								"style": 5,
								"label": "📷 Media",
								"url": "https://discord.com/channels/1265120128295632926/1265122765279727657"
							},
							{
								"type": 2,
								"style": 5,
								"label": "🎮 Game Clips",
								"url": "https://discord.com/channels/1265120128295632926/1265123462284836935"
							},
							{
								"type": 2,
								"style": 5,
								"label": "💬 Gamer Chat",
								"url": "https://discord.com/channels/1265120128295632926/1265123424892616705"
							}
						]
					},
					{
						"type": 14,  # Separator
						"divider": True,
						"spacing": 1
					},
					{
						"type": 10,  # Text block
						"content": (
							"**Explore and have fun!**\n"
							"- Play games like UNO, TicTacToe, Hangman\n"
							"- Compete in leaderboards\n"
							"- Join voice chat and events 🎤"
						)
					}
				]
			}
		]
	}

	async with aiohttp.ClientSession() as session:
		async with session.post(url, headers=headers, json=json_payload) as resp:
			logger.info(f"\n{s}[WELCOME] Status: {resp.status}\n")
			if resp.status != 200:
				logger.error(f"Failed to send welcome message: {await resp.text()}")


# Keep your existing event handlers
@bot.event
async def on_interaction(interaction: discord.Interaction):
	author_id = interaction.user.id
	if interaction.type == discord.InteractionType.component:
		if interaction.data["custom_id"] == "Need Help?":
			# Get the embed and view from the get_help_menu
			embed, view = await get_help_menu(author_id)
			# Send the response
			await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.event
async def on_member_remove(member: discord.Member):
	logger.info(f"\n{s}Member left: {member.name} ({member.id}) in guild: {member.guild.name}\n")

	# Clean up rate limit data for users who leave
	if member.id in dm_rate_limits:
		del dm_rate_limits[member.id]
		logger.info(f"Cleaned up rate limit data for {member.id}")

	try:
		# Update members cache for this guild
		await cache_manager.cache_members(member.guild)
		logger.info(f"\n{s}Member cache updated for {member.guild.name}\n")
	except Exception as e:
		logger.error(f"\n{s}Error updating member cache for {member.guild.name}: {e}\n")


@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
	logger.info(f"\n{s}Role updated: {after.name} ({after.id}) in guild: {after.guild.name}\n")
	try:
		# Update roles cache for this guild
		await cache_manager.cache_roles(after.guild)
		logger.info(f"\n{s}Roles cache updated for {after.guild.name}\n")
	except Exception as e:
		logger.error(f"Error updating roles cache for {after.guild.name}: {e}\n")


@bot.event
async def on_guild_channel_update(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
	logger.info(f"\n{s}Channel updated: {after.name} ({after.id}) in guild: {after.guild.name}\n")
	try:
		# Update channels cache for this guild
		await cache_manager.cache_channels(after.guild)
		logger.info(f"\n{s}Channels cache updated for {after.guild.name}\n")
	except Exception as e:
		logger.error(f"\n{s}Error updating channels cache for {after.guild.name}: {e}\n")