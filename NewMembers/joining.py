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


class GuildEventHandler:
	"""Handles all guild-related events with enhanced caching and rate limiting"""

	def __init__(self, bot, cache_manager):
		self.bot = bot
		self.cache_manager = cache_manager
		self.logger = get_logger("GuildEventHandler")

		# Enhanced guild-specific rate limiting storage
		self.dm_rate_limits: Dict[int, Dict[str, Any]] = defaultdict(lambda: {
			'count': 0,
			'last_reset': datetime.now(timezone.utc),
			'blocked_until': None,
			'total_attempts': 0,
			'first_attempt': None
		})

		# Enhanced guild cache with more comprehensive data
		self.guild_cache: Dict[int, Dict[str, Any]] = defaultdict(lambda: {
			'member_count': 0,
			'bot_count': 0,
			'online_count': 0,
			'new_member_joins_today': 0,
			'kicks_today': 0,
			'last_activity': None,
			'voice_channels_active': 0,
			'recent_messages': 0,
			'moderation_actions': [],
			'member_retention_rate': 0.0,
			'popular_channels': {},
			'role_distribution': {},
			'timezone_distribution': {},
			'join_patterns': {
				'hourly': defaultdict(int),
				'daily': defaultdict(int),
				'weekly': defaultdict(int)
			},
			'security_metrics': {
				'suspicious_joins': 0,
				'account_age_violations': 0,
				'rapid_joins': 0
			}
		})

		# Rate limit configuration - can be guild-specific
		self.rate_limits = {
			'new_account_days': 30,
			'max_dms_per_hour': 2,
			'max_dms_per_day': 5,
			'block_duration_hours': 24,
		}

	async def initialize_guild_cache(self, guild: discord.Guild):
		"""Initialize comprehensive guild cache data"""
		try:
			self.logger.info(f"Initializing comprehensive guild cache for {guild.name} ({guild.id})")

			cache_data = self.guild_cache[guild.id]

			# Basic guild metrics
			cache_data['member_count'] = guild.member_count
			cache_data['bot_count'] = sum(1 for member in guild.members if member.bot)
			cache_data['online_count'] = sum(1 for member in guild.members
											 if hasattr(member, 'status') and member.status != discord.Status.offline)

			# Voice channel activity
			cache_data['voice_channels_active'] = sum(1 for vc in guild.voice_channels if vc.members)

			# Role distribution
			role_dist = defaultdict(int)
			for member in guild.members:
				for role in member.roles:
					if not role.is_default():
						role_dist[role.name] += 1
			cache_data['role_distribution'] = dict(role_dist)

			# Channel activity estimation (simplified)
			channel_activity = {}
			for channel in guild.text_channels:
				try:
					# Get recent message count (last 24 hours)
					recent_count = 0
					async for message in channel.history(after=datetime.now(timezone.utc) - timedelta(hours=24),
														 limit=100):
						recent_count += 1
					channel_activity[channel.name] = recent_count
				except (discord.Forbidden, discord.HTTPException):
					channel_activity[channel.name] = 0

			cache_data['popular_channels'] = dict(sorted(channel_activity.items(),
														 key=lambda x: x[1], reverse=True)[:5])

			# Initialize today's counters
			cache_data['new_member_joins_today'] = 0
			cache_data['kicks_today'] = 0
			cache_data['last_activity'] = datetime.now(timezone.utc).isoformat()

			# Security metrics initialization
			cache_data['security_metrics']['suspicious_joins'] = 0
			cache_data['security_metrics']['account_age_violations'] = 0
			cache_data['security_metrics']['rapid_joins'] = 0

			self.logger.info(f"Guild cache initialized: {guild.member_count} members, "
							 f"{cache_data['bot_count']} bots, {cache_data['online_count']} online")

		except Exception as e:
			self.logger.error(f"Error initializing guild cache for {guild.name}: {e}")

	async def update_guild_metrics(self, guild: discord.Guild, event_type: str, **kwargs):
		"""Update guild metrics based on events"""
		try:
			cache_data = self.guild_cache[guild.id]
			now = datetime.now(timezone.utc)

			if event_type == "member_join":
				cache_data['new_member_joins_today'] += 1
				cache_data['member_count'] = guild.member_count

				# Track join patterns
				hour = now.hour
				day = now.strftime('%Y-%m-%d')
				week = now.strftime('%Y-W%U')

				cache_data['join_patterns']['hourly'][hour] += 1
				cache_data['join_patterns']['daily'][day] += 1
				cache_data['join_patterns']['weekly'][week] += 1

				# Check for rapid joins (security metric)
				recent_joins = cache_data['join_patterns']['hourly'][hour]
				if recent_joins > 10:  # More than 10 joins in an hour
					cache_data['security_metrics']['rapid_joins'] += 1

				member = kwargs.get('member')
				if member:
					account_age = now - member.created_at
					if account_age.days < self.rate_limits['new_account_days']:
						cache_data['security_metrics']['account_age_violations'] += 1

			elif event_type == "member_remove":
				cache_data['member_count'] = guild.member_count

			elif event_type == "member_kick":
				cache_data['kicks_today'] += 1

			cache_data['last_activity'] = now.isoformat()

			# Update database cache periodically
			await self.cache_manager.cache_guild_info(guild)

		except Exception as e:
			self.logger.error(f"Error updating guild metrics for {guild.name}: {e}")

	async def get_guild_analytics(self, guild_id: int) -> Dict[str, Any]:
		"""Get comprehensive guild analytics"""
		cache_data = self.guild_cache.get(guild_id, {})

		analytics = {
			'basic_stats': {
				'total_members': cache_data.get('member_count', 0),
				'bot_count': cache_data.get('bot_count', 0),
				'human_members': cache_data.get('member_count', 0) - cache_data.get('bot_count', 0),
				'online_members': cache_data.get('online_count', 0)
			},
			'activity_stats': {
				'joins_today': cache_data.get('new_member_joins_today', 0),
				'kicks_today': cache_data.get('kicks_today', 0),
				'active_voice_channels': cache_data.get('voice_channels_active', 0),
				'popular_channels': cache_data.get('popular_channels', {})
			},
			'security_metrics': cache_data.get('security_metrics', {}),
			'join_patterns': cache_data.get('join_patterns', {}),
			'role_distribution': cache_data.get('role_distribution', {}),
			'last_updated': cache_data.get('last_activity')
		}

		return analytics

	async def can_send_dm(self, member: discord.Member) -> tuple[bool, str]:
		"""Enhanced DM rate limiting with guild-specific tracking"""
		now = datetime.now(timezone.utc)
		account_age = now - member.created_at

		# Only apply rate limits to new accounts
		if account_age.days >= self.rate_limits['new_account_days']:
			return True, "Account old enough"

		user_limits = self.dm_rate_limits[member.id]

		# Initialize first attempt tracking
		if user_limits['first_attempt'] is None:
			user_limits['first_attempt'] = now

		user_limits['total_attempts'] += 1

		# Check if user is currently blocked
		if user_limits.get('blocked_until') and now < user_limits['blocked_until']:
			remaining = user_limits['blocked_until'] - now
			return False, f"Blocked for {remaining.seconds // 3600}h {(remaining.seconds % 3600) // 60}m"

		# Reset counters if needed (hourly reset)
		if now - user_limits['last_reset'] >= timedelta(hours=1):
			user_limits['count'] = 0
			user_limits['last_reset'] = now
			user_limits['blocked_until'] = None

		# Check hourly limit
		if user_limits['count'] >= self.rate_limits['max_dms_per_hour']:
			# Block the user
			user_limits['blocked_until'] = now + timedelta(hours=self.rate_limits['block_duration_hours'])
			self.logger.warning(
				f"User {member} ({member.id}) hit DM rate limit, blocked for {self.rate_limits['block_duration_hours']} hours"
			)
			return False, "Rate limit exceeded"

		return True, "Within limits"

	async def record_dm_sent(self, member: discord.Member):
		"""Record that a DM was sent with enhanced tracking"""
		user_limits = self.dm_rate_limits[member.id]
		user_limits['count'] += 1

		# Update guild security metrics
		await self.update_guild_metrics(
			member.guild,
			"dm_sent",
			member=member,
			reason="account_age_restriction"
		)

		self.logger.info(f"DM count for {member} ({member.id}): {user_limits['count']}")

	async def handle_member_join(self, member: discord.Member):
		"""Handle member join with comprehensive tracking and caching"""
		self.logger.info(f"\n{s}New member joined: {member} ({member.id}) in {member.guild.name}\n")

		now = datetime.now(timezone.utc)
		account_age = now - member.created_at
		guild = member.guild

		# Update guild metrics
		await self.update_guild_metrics(guild, "member_join", member=member)

		# Initialize guild cache if needed
		if guild.id not in self.guild_cache:
			await self.initialize_guild_cache(guild)

		# Default avatar fallback
		avatar_url = member.display_avatar.url if member.display_avatar else "https://cdn.discordapp.com/embed/avatars/0.png"

		if account_age.days < 90:
			# Account is too new — check if we can send DM with rate limiting
			can_dm, reason = await self.can_send_dm(member)

			if can_dm:
				try:
					await member.send(
						f"Hey {member.name}! 👋\n\n"
						f"Unfortunately, your Discord account is too new to join our server (created {account_age.days} days ago).\n"
						f"We require accounts to be a certain number of days old to help prevent spam and protect our community.\n\n"
						f"You're welcome to try again once your account is older. Thanks for understanding! 🙏"
					)
					await self.record_dm_sent(member)
					self.logger.info(f"\n{s}Sent DM to {member} about new account restriction.\n")
				except discord.Forbidden:
					self.logger.warning(f"\n{s}Could not DM {member} before kick (Forbidden).\n")
				except Exception as e:
					self.logger.error(f"\n{s}Failed to DM {member}: {e}")
			else:
				self.logger.info(f"\n{s}Skipped DM to {member} due to rate limiting: {reason}\n")

			try:
				await asyncio.sleep(1.2)
				await member.kick(reason=f"Account too new ({account_age.days} days old)")
				await self.update_guild_metrics(guild, "member_kick", member=member)
				self.logger.info(f"\n{s}Kicked {member} due to account age ({account_age.days} days).\n")
			except Exception as e:
				self.logger.error(f"\n{s}Failed to kick {member}: {e}\n")

			await asyncio.sleep(1.2)
			return

		# Account is old enough - proceed with welcome
		channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
		if channel:
			try:
				# Update members cache for this guild
				await self.cache_manager.cache_members(member.guild)
				self.logger.info(f"\n{s}Member cache updated for {member.guild.name}\n")
			except Exception as e:
				self.logger.error(f"\n{s}Error updating member cache for {member.guild.name}: {e}\n")

			try:
				await asyncio.sleep(1.2)
				await self.send_welcome_message(member)
				self.logger.info(f"Interactive welcome message sent for {member}\n")
			except Exception as e:
				self.logger.error(f"Error sending welcome message: {e}\n")

	async def send_welcome_message(self, member: discord.Member, avatar_url: str = None):
		"""Send enhanced welcome message with guild-specific data"""
		url = f"https://discord.com/api/v10/channels/{WELCOME_CHANNEL_ID}/messages"
		if avatar_url is None:
			avatar_url = member.display_avatar.url if member.display_avatar else "https://cdn.discordapp.com/embed/avatars/0.png"

		headers = {
			'Authorization': f"Bot {TOKEN}",
			'Content-Type': 'application/json'
		}

		# Get guild analytics for personalized welcome
		analytics = await self.get_guild_analytics(member.guild.id)
		member_number = analytics['basic_stats']['total_members']

		json_payload = {
			"flags": 1 << 15,
			"components": [
				{
					"type": 17,
					"accent_color": 0x5865F2,
					"components": [
						{
							"type": 12,
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
							"type": 10,
							"content": f"# Welcome to the server, <@{member.id}>!\n*You're member #{member_number}!*"
						},
						{
							"type": 1,
							"components": [
								{
									"type": 2,
									"label": "✅ Guide",
									"style": 3,
									"custom_id": "Need Help?"
								},
								{
									"type": 2,
									"label": "📜 Rules",
									"style": 5,
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
							"content": (f":wave: Welcome to the Discord server!\n"
										f"We are a community of {analytics['basic_stats']['human_members']} gamers and love that you made it here.\n"
										f"Feedback is welcome and helpful.\n"
										f"Use `/suggest` to start your suggestion in #suggestions.\n"
										f"Don't want your name on it? Use the anonymous option!\n")
						},
						{
							"type": 14,
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
							"type": 14,
							"divider": True,
							"spacing": 1
						},
						{
							"type": 10,
							"content": (
								"**Explore and have fun!**\n"
								"- Play games like UNO, TicTacToe, Hangman\n"
								"- Compete in leaderboards\n"
								f"- Join voice chat and events 🎤 ({analytics['activity_stats']['active_voice_channels']} active now!)"
							)
						}
					]
				}
			]
		}

		async with aiohttp.ClientSession() as session:
			async with session.post(url, headers=headers, json=json_payload) as resp:
				self.logger.info(f"\n{s}[WELCOME] Status: {resp.status}\n")
				if resp.status != 200:
					self.logger.error(f"Failed to send welcome message: {await resp.text()}")

	async def handle_interaction(self, interaction: discord.Interaction):
		"""Handle button interactions"""
		author_id = interaction.user.id
		if interaction.type == discord.InteractionType.component:
			if interaction.data["custom_id"] == "Need Help?":
				from Guide.guide import get_help_menu
				embed, view = await get_help_menu(author_id)
				await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

	async def handle_member_remove(self, member: discord.Member):
		"""Handle member removal with enhanced tracking"""
		self.logger.info(f"\n{s}Member left: {member.name} ({member.id}) in guild: {member.guild.name}\n")

		# Update guild metrics
		await self.update_guild_metrics(member.guild, "member_remove", member=member)

		# Clean up rate limit data for users who leave
		if member.id in self.dm_rate_limits:
			del self.dm_rate_limits[member.id]
			self.logger.info(f"Cleaned up rate limit data for {member.id}")

		try:
			# Update members cache for this guild
			await self.cache_manager.cache_members(member.guild)
			self.logger.info(f"\n{s}Member cache updated for {member.guild.name}\n")
		except Exception as e:
			self.logger.error(f"\n{s}Error updating member cache for {member.guild.name}: {e}\n")

	async def handle_guild_role_update(self, before: discord.Role, after: discord.Role):
		"""Handle role updates with caching"""
		self.logger.info(f"\n{s}Role updated: {after.name} ({after.id}) in guild: {after.guild.name}\n")

		# Update guild role distribution cache
		guild_data = self.guild_cache[after.guild.id]
		role_dist = defaultdict(int)
		for member in after.guild.members:
			for role in member.roles:
				if not role.is_default():
					role_dist[role.name] += 1
		guild_data['role_distribution'] = dict(role_dist)

		try:
			# Update roles cache for this guild
			await self.cache_manager.cache_roles(after.guild)
			self.logger.info(f"\n{s}Roles cache updated for {after.guild.name}\n")
		except Exception as e:
			self.logger.error(f"Error updating roles cache for {after.guild.name}: {e}\n")

	async def handle_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
		"""Handle channel updates with caching"""
		self.logger.info(f"\n{s}Channel updated: {after.name} ({after.id}) in guild: {after.guild.name}\n")
		try:
			# Update channels cache for this guild
			await self.cache_manager.cache_channels(after.guild)
			self.logger.info(f"\n{s}Channels cache updated for {after.guild.name}\n")
		except Exception as e:
			self.logger.error(f"\n{s}Error updating channels cache for {after.guild.name}: {e}\n")


# Create the guild event handler instance
guild_handler = GuildEventHandler(bot, cache_manager)


# Keep your existing event handlers but delegate to the class

# Section: Interactions
# Missing in this section:
# - (None; all handled via on_interaction)
@bot.event
async def on_interaction(interaction: discord.Interaction):
	await guild_handler.handle_interaction(interaction)

# Section: Member lifecycle and moderation
# Missing in this section:
# - on_member_chunk
@bot.event
async def on_member_join(member):
	await guild_handler.handle_member_join(member)

@bot.event
async def on_member_remove(member: discord.Member):
	await guild_handler.handle_member_remove(member)

@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role):
	await guild_handler.handle_guild_role_update(before, after)

@bot.event
async def on_guild_channel_update(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
	await guild_handler.handle_guild_channel_update(before, after)

@bot.event
async def on_member_update(before, after):
	#ToDO
	guild_handler.logger.info(f"Event: on_member_update - {before.id} in {getattr(before, 'guild', 'N/A')}")
	pass

@bot.event
async def on_member_ban(guild, user):
	#ToDO
	guild_handler.logger.info(f"Event: on_member_ban - {user} in {guild.name} ({guild.id})")
	pass

@bot.event
async def on_member_unban(guild, user):
	#ToDO
	guild_handler.logger.info(f"Event: on_member_unban - {user} in {guild.name} ({guild.id})")
	pass

# Section: Connection / Lifecycle
# Missing in this section:
# - on_ready
# - on_shard_connect
# - on_shard_disconnect
# - on_shard_ready
# - on_shard_resumed
@bot.event
async def on_connect():
	#ToDO
	guild_handler.logger.info("Event: on_connect - connected to gateway")
	pass

@bot.event
async def on_disconnect():
	#ToDO
	guild_handler.logger.info("Event: on_disconnect - disconnected from gateway")
	pass

@bot.event
async def on_resumed():
	#ToDO
	guild_handler.logger.info("Event: on_resumed - session resumed")
	pass

# Section: Guild lifecycle and cache
# Missing in this section:
# - on_guild_integrations_update
# - on_audit_log_entry_create
@bot.event
async def on_guild_join(guild):
	guild_handler.logger.info(f"Event: on_guild_join - joined guild {guild.name} ({guild.id})")
	# Optionally initialize cache for new guild
	try:
		await guild_handler.initialize_guild_cache(guild)
	except Exception as e:
		guild_handler.logger.error(f"Error initializing cache on guild join: {e}")
	pass

@bot.event
async def on_guild_remove(guild):
	guild_handler.logger.info(f"Event: on_guild_remove - removed from guild {guild.name} ({guild.id})")
	# Clean up guild cache if present
	if guild.id in guild_handler.guild_cache:
		del guild_handler.guild_cache[guild.id]
		guild_handler.logger.info(f"Cleared cache for guild {guild.id}")
	pass

@bot.event
async def on_guild_update(before, after):
	#ToDO
	guild_handler.logger.info(f"Event: on_guild_update - {after.name} ({after.id})")
	pass

@bot.event
async def on_guild_available(guild):
	#ToDO
	guild_handler.logger.info(f"Event: on_guild_available - {guild.name} ({guild.id})")
	pass

@bot.event
async def on_guild_unavailable(guild):
	#ToDO
	guild_handler.logger.info(f"Event: on_guild_unavailable - {guild.name} ({guild.id})")
	pass

# Section: Roles
# Missing in this section:
# - (None)
@bot.event
async def on_guild_role_create(role):
	guild_handler.logger.info(f"Event: on_guild_role_create - {role.name} ({role.id}) in {role.guild.name}")
	# update role distribution cache
	try:
		await guild_handler.handle_guild_role_update(role, role)
	except Exception:
		# fallback to cache refresh
		await guild_handler.cache_manager.cache_roles(role.guild)
	pass

@bot.event
async def on_guild_role_delete(role):
	guild_handler.logger.info(f"Event: on_guild_role_delete - {role.name} ({role.id}) in {role.guild.name}")
	# refresh roles cache
	try:
		await guild_handler.cache_manager.cache_roles(role.guild)
	except Exception as e:
		guild_handler.logger.error(f"Error updating roles cache after delete: {e}")
	pass

# Section: Emojis and Stickers
# Missing in this section:
# - on_guild_stickers_update
@bot.event
async def on_guild_emojis_update(guild, before, after):
	#ToDO
	guild_handler.logger.info(f"Event: on_guild_emojis_update - {guild.name} ({guild.id})")
	pass

# Section: Webhooks and Integrations
# Missing in this section:
# - on_integration_create
# - on_integration_update
@bot.event
async def on_webhooks_update(channel):
	#ToDO
	guild_handler.logger.info(f"Event: on_webhooks_update - channel {getattr(channel, 'name', channel.id)}")
	pass

# Section: Channels
# Missing in this section:
# - on_guild_channel_pins_update
# - on_private_channel_create
# - on_private_channel_delete
# - on_private_channel_update
# - on_private_channel_pins_update
@bot.event
async def on_channel_create(channel):
	#ToDO
	guild_handler.logger.info(f"Event: on_channel_create - {getattr(channel, 'name', channel.id)} in {getattr(channel, 'guild', 'DM')}")
	pass

@bot.event
async def on_channel_delete(channel):
	#ToDO
	guild_handler.logger.info(f"Event: on_channel_delete - {getattr(channel, 'name', channel.id)}")
	pass

# Section: Threads
# Missing in this section:
# - on_thread_member_join
# - on_thread_member_remove
@bot.event
async def on_thread_create(thread):
	#ToDO
	guild_handler.logger.info(f"Event: on_thread_create - {thread.name} ({thread.id})")
	pass

@bot.event
async def on_thread_update(before, after):
	#ToDO
	guild_handler.logger.info(f"Event: on_thread_update - {after.name} ({after.id})")
	pass

@bot.event
async def on_thread_delete(thread):
	#ToDO
	guild_handler.logger.info(f"Event: on_thread_delete - {thread.name} ({thread.id})")
	pass

# Section: Voice and Presence
# Missing in this section:
# - (None)
@bot.event
async def on_voice_state_update(member, before, after):
	guild_handler.logger.info(f"Event: on_voice_state_update - {member} in {member.guild.name}")
	# update voice channel counts in cache if present
	try:
		await guild_handler.update_guild_metrics(member.guild, "voice_state_change", member=member)
	except Exception:
		pass
	pass

@bot.event
async def on_presence_update(before, after):
	#ToDO
	guild_handler.logger.info(f"Event: on_presence_update - {getattr(after, 'id', 'N/A')}")
	pass

# Section: Users and Typing
# Missing in this section:
# - (None)
@bot.event
async def on_user_update(before, after):
	#ToDO
	guild_handler.logger.info(f"Event: on_user_update - {after.id}")
	pass

@bot.event
async def on_typing(channel, user, when):
	#ToDO
	guild_handler.logger.info(f"Event: on_typing - {user} in {getattr(channel, 'name', channel.id)} at {when}")
	pass

# Section: Messages
# Missing in this section:
# - on_bulk_message_delete
# - on_raw_message_edit
# - on_raw_bulk_message_delete
@bot.event
async def on_message(message):
	#ToDO
	# Ignore bot messages
	if message.author.bot:
		return
	guild_handler.logger.info(f"Event: on_message - {message.author} in {getattr(message.guild, 'name', 'DM')}")
	# Keep default processing intact (commands, etc.)
	await bot.process_commands(message)

@bot.event
async def on_message_edit(before, after):
	#ToDO
	guild_handler.logger.info(f"Event: on_message_edit - message {before.id}")
	pass

@bot.event
async def on_message_delete(message):
	#ToDO
	guild_handler.logger.info(f"Event: on_message_delete - message {getattr(message, 'id', 'raw')}")
	pass

@bot.event
async def on_raw_message_delete(payload):
	#ToDO
	guild_handler.logger.info(f"Event: on_raw_message_delete - id {payload.message_id}")
	pass

@bot.event
async def on_message_delete_bulk(messages):
	#ToDO
	guild_handler.logger.info(f"Event: on_message_delete_bulk - {len(messages)} messages")
	pass

# Section: Reactions
# Missing in this section:
# - on_reaction_clear_emoji
# - on_raw_reaction_clear
# - on_raw_reaction_clear_emoji
@bot.event
async def on_reaction_add(reaction, user):
	#ToDO
	guild_handler.logger.info(f"Event: on_reaction_add - {user} reacted in {getattr(reaction.message, 'id', 'N/A')}")
	pass

@bot.event
async def on_reaction_remove(reaction, user):
	#ToDO
	guild_handler.logger.info(f"Event: on_reaction_remove - {user} removed reaction")
	pass

@bot.event
async def on_reaction_clear(message, reactions):
	#ToDO
	guild_handler.logger.info(f"Event: on_reaction_clear - cleared on message {getattr(message, 'id', 'N/A')}")
	pass

@bot.event
async def on_raw_reaction_add(payload):
	#ToDO
	guild_handler.logger.info(f"Event: on_raw_reaction_add - {payload}")
	pass

@bot.event
async def on_raw_reaction_remove(payload):
	#ToDO
	guild_handler.logger.info(f"Event: on_raw_reaction_remove - {payload}")
	pass

# Section: Invites
# Missing in this section:
# - (None)
@bot.event
async def on_invite_create(invite):
	#ToDO
	guild_handler.logger.info(f"Event: on_invite_create - {invite.code} to {invite.guild}")
	pass

@bot.event
async def on_invite_delete(invite):
	#ToDO
	guild_handler.logger.info(f"Event: on_invite_delete - invite deleted")
	pass