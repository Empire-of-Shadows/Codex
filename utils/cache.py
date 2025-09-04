import os
import asyncio
from typing import List, Dict, Optional, Set
from datetime import datetime, timezone, timedelta
import discord
import pendulum
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase, AsyncIOMotorCollection
import logging
from pymongo import UpdateOne
from collections import defaultdict

from utils.logger import get_logger

logger = get_logger("GuildCacheManager")


class GuildCacheManager:
	def __init__(self, mongo_uri: str):
		"""
		Initialize the cache manager with direct database connection.

		Args:
			mongo_uri: MongoDB connection URI for the cache database
		"""
		self.mongo_uri = mongo_uri
		self._client: Optional[AsyncIOMotorClient] = None
		self._db: Optional[AsyncIOMotorDatabase] = None
		self._channels: Optional[AsyncIOMotorCollection] = None
		self._servers: Optional[AsyncIOMotorCollection] = None
		self._roles: Optional[AsyncIOMotorCollection] = None
		self._members: Optional[AsyncIOMotorCollection] = None
		self._analytics: Optional[AsyncIOMotorCollection] = None  # New analytics collection
		self._events: Optional[AsyncIOMotorCollection] = None  # New events tracking collection
		self._cache_locks = {}  # Per-guild locks for thread safety
		self._initialized = False

		# Enhanced real-time cache for frequently accessed data
		self._memory_cache = {
			'guild_stats': {},
			'member_counts': {},
			'active_channels': {},
			'recent_events': defaultdict(list)
		}

	async def initialize(self):
		"""Initialize the database connection and collections."""
		if self._initialized:
			return

		try:
			self._client = AsyncIOMotorClient(self.mongo_uri)
			self._db = self._client["ServerData"]

			# Initialize collections
			self._channels = self._db["Channels"]
			self._servers = self._db["Guilds"]
			self._members = self._db["Members"]
			self._roles = self._db["Roles"]
			self._analytics = self._db["Analytics"]
			self._events = self._db["Events"]

			# Test the connection
			await self._client.admin.command('ping')

			# Create indexes for better performance
			await self._create_indexes()

			self._initialized = True
			logger.info("GuildCacheManager database connection initialized successfully")

		except Exception as e:
			logger.error(f"Failed to initialize GuildCacheManager database connection: {e}")
			raise

	async def _create_indexes(self):
		"""Create database indexes for optimal performance"""
		try:
			# Guild-based indexes
			await self._channels.create_index([("guild_id", 1), ("type", 1)])
			await self._members.create_index([("guild_id", 1), ("bot", 1)])
			await self._roles.create_index([("guild_id", 1), ("position", 1)])
			await self._analytics.create_index([("guild_id", 1), ("date", -1)])
			await self._events.create_index([("guild_id", 1), ("timestamp", -1)])

			# Time-based indexes for analytics
			await self._analytics.create_index([("date", -1)])
			await self._events.create_index([("timestamp", -1)])

			logger.debug("Database indexes created successfully")
		except Exception as e:
			logger.warning(f"Error creating indexes: {e}")

	async def close(self):
		"""Close the database connection."""
		if self._client:
			self._client.close()
			self._initialized = False
			logger.info("GuildCacheManager database connection closed")

	def _ensure_initialized(self):
		"""Ensure the database connection is initialized."""
		if not self._initialized:
			raise RuntimeError("GuildCacheManager not initialized. Call initialize() first.")

	@property
	def channels(self) -> AsyncIOMotorCollection:
		"""Get the channels collection."""
		self._ensure_initialized()
		return self._channels

	@property
	def servers(self) -> AsyncIOMotorCollection:
		"""Get the servers collection."""
		self._ensure_initialized()
		return self._servers

	@property
	def roles(self) -> AsyncIOMotorCollection:
		"""Get the roles collection."""
		self._ensure_initialized()
		return self._roles

	@property
	def members(self) -> AsyncIOMotorCollection:
		"""Get the members collection."""
		self._ensure_initialized()
		return self._members

	@property
	def analytics(self) -> AsyncIOMotorCollection:
		"""Get the analytics collection."""
		self._ensure_initialized()
		return self._analytics

	@property
	def events(self) -> AsyncIOMotorCollection:
		"""Get the events collection."""
		self._ensure_initialized()
		return self._events

	def _get_guild_lock(self, guild_id: int) -> asyncio.Lock:
		"""Get or create a lock for a specific guild to prevent race conditions."""
		if guild_id not in self._cache_locks:
			self._cache_locks[guild_id] = asyncio.Lock()
		return self._cache_locks[guild_id]

	async def cache_all(self, guild: discord.Guild, force_refresh: bool = False):
		"""Cache all guild data with optional force refresh and better error handling."""
		self._ensure_initialized()

		async with self._get_guild_lock(guild.id):
			try:
				logger.info(f"Starting cache operation for guild {guild.name} ({guild.id})")

				# Check if we need to refresh based on last update time
				if not force_refresh and not await self._should_refresh_cache(guild):
					logger.info(f"Cache for guild {guild.name} is still fresh, skipping")
					return

				# Run all caching operations concurrently for better performance
				await asyncio.gather(
					self.cache_guild_info(guild),
					self.cache_channels(guild),
					self.cache_roles(guild),
					self.cache_members(guild),
					self.cache_guild_analytics(guild),
					return_exceptions=True
				)

				logger.info(f"Completed cache operation for guild {guild.name} ({guild.id})")

			except Exception as e:
				logger.error(f"Error caching guild {guild.name} ({guild.id}): {e}")
				raise

	async def _should_refresh_cache(self, guild: discord.Guild) -> bool:
		"""Check if cache needs refreshing based on last update time."""
		try:
			last_cached = await self.servers.find_one(
				{"id": guild.id},
				{"updated_at": 1}
			)

			if not last_cached or "updated_at" not in last_cached:
				return True

			# Refresh if older than 1 hour
			last_update = pendulum.parse(last_cached["updated_at"])
			return pendulum.now("America/Chicago").diff(last_update).in_hours() >= 1

		except Exception as e:
			logger.warning(f"Error checking cache freshness for guild {guild.id}: {e}")
			return True  # Refresh on error

	async def cache_guild_info(self, guild: discord.Guild):
		"""Enhanced guild info caching with additional metadata and analytics integration."""
		try:
			# Get additional guild features and settings
			features = list(guild.features) if guild.features else []

			# Calculate enhanced metrics
			bot_count = sum(1 for member in guild.members if member.bot)
			online_count = sum(1 for member in guild.members
							   if hasattr(member, 'status') and member.status != discord.Status.offline)
			voice_channels_active = sum(1 for vc in guild.voice_channels if vc.members)

			# Get premium subscriber count
			premium_members = sum(1 for member in guild.members if member.premium_since)

			data = {
				"id": guild.id,
				"name": guild.name,
				"icon_url": str(guild.icon.url) if guild.icon else None,
				"banner_url": str(guild.banner.url) if guild.banner else None,
				"description": guild.description,
				"owner_id": guild.owner_id,
				"member_count": guild.member_count,
				"bot_count": bot_count,
				"human_count": guild.member_count - bot_count,
				"online_count": online_count,
				"premium_members": premium_members,
				"voice_channels_active": voice_channels_active,
				"max_members": guild.max_members,
				"verification_level": str(guild.verification_level),
				"default_notifications": str(guild.default_notifications),
				"explicit_content_filter": str(guild.explicit_content_filter),
				"mfa_level": guild.mfa_level,
				"premium_tier": guild.premium_tier,
				"premium_subscription_count": guild.premium_subscription_count,
				"features": features,
				"created_at": guild.created_at.isoformat(),
				"updated_at": pendulum.now("America/Chicago").isoformat(),
				"cache_version": "3.0",  # Updated version

				# Enhanced metadata
				"total_channels": len(guild.channels),
				"text_channels": len(guild.text_channels),
				"voice_channels": len(guild.voice_channels),
				"categories": len(guild.categories),
				"total_roles": len(guild.roles),
				"system_channel_id": guild.system_channel.id if guild.system_channel else None,
				"rules_channel_id": guild.rules_channel.id if guild.rules_channel else None,
				"public_updates_channel_id": guild.public_updates_channel.id if guild.public_updates_channel else None,
				"vanity_url": guild.vanity_url,
				"preferred_locale": str(guild.preferred_locale) if guild.preferred_locale else None,
			}

			await self.servers.update_one(
				{"id": guild.id},
				{"$set": data},
				upsert=True
			)

			# Update memory cache
			self._memory_cache['guild_stats'][guild.id] = {
				'member_count': guild.member_count,
				'bot_count': bot_count,
				'online_count': online_count,
				'voice_active': voice_channels_active,
				'last_update': datetime.now(timezone.utc)
			}

			logger.debug(f"Cached enhanced guild info for {guild.name}")

		except Exception as e:
			logger.error(f"Error caching guild info for {guild.name}: {e}")
			raise

	async def cache_channels(self, guild: discord.Guild):
		"""Enhanced channel caching with better categorization and thread support."""
		try:
			cached_channels = []

			for channel in guild.channels:
				try:
					# Prepare permissions data with better error handling
					permissions = []
					try:
						# channel.overwrites is a mapping: target (Role|Member) -> PermissionOverwrite
						for target, overwrite in (channel.overwrites or {}).items():
							try:
								# Build allow/deny bitfields from the PermissionOverwrite
								allow = discord.Permissions.none()
								deny = discord.Permissions.none()
								for name, value in overwrite:
									if value is True:
										setattr(allow, name, True)
									elif value is False:
										setattr(deny, name, True)

								permissions.append({
									"id": target.id,
									"name": getattr(target, "name", None),
									"type": "role" if isinstance(target, discord.Role) else "user",
									"allow": allow.value,
									"deny": deny.value,
								})
							except Exception as po_err:
								logger.debug(f"Skipping bad overwrite for channel {channel.name}: {po_err}")
					except Exception as perm_error:
						logger.warning(f"Error processing permissions for channel {channel.name}: {perm_error}")

					# Base channel data
					channel_data = {
						"guild_id": guild.id,
						"id": channel.id,
						"name": channel.name,
						"type": str(channel.type),
						"position": channel.position,
						"permissions": permissions,
						"created_at": channel.created_at.isoformat(),
						"updated_at": pendulum.now("America/Chicago").isoformat(),
					}

					# Add category-specific data
					if hasattr(channel, 'category') and channel.category:
						channel_data["category_id"] = channel.category.id
						channel_data["category_name"] = channel.category.name

					# Add text channel specific data
					if isinstance(channel, discord.TextChannel):
						channel_data.update({
							"topic": channel.topic,
							"slowmode_delay": channel.slowmode_delay,
							"nsfw": channel.nsfw,
							"last_message_id": channel.last_message_id,
							"message_history_enabled": True,  # Assume enabled unless proven otherwise
						})

						# Cache active threads if any
						if hasattr(channel, 'threads'):
							threads = []
							try:
								async for thread in channel.archived_threads(limit=50):
									threads.append({
										"id": thread.id,
										"name": thread.name,
										"archived": thread.archived,
										"locked": thread.locked,
										"created_at": thread.created_at.isoformat()
									})
								channel_data["archived_threads"] = threads
								channel_data["thread_count"] = len(threads)
							except (discord.Forbidden, discord.HTTPException):
								channel_data["archived_threads"] = []
								channel_data["thread_count"] = 0

					# Add voice channel specific data
					elif isinstance(channel, discord.VoiceChannel):
						channel_data.update({
							"bitrate": channel.bitrate,
							"user_limit": channel.user_limit,
							"rtc_region": str(channel.rtc_region) if channel.rtc_region else None,
							"current_users": len(channel.members),
							"user_list": [member.id for member in channel.members]
						})

					# Add forum channel specific data
					elif hasattr(discord, 'ForumChannel') and isinstance(channel, discord.ForumChannel):
						channel_data.update({
							"topic": channel.topic,
							"slowmode_delay": channel.slowmode_delay,
							"nsfw": channel.nsfw,
							"default_auto_archive_duration": channel.default_auto_archive_duration,
						})

					cached_channels.append(channel_data)

				except Exception as channel_error:
					logger.error(f"Error processing channel {channel.name}: {channel_error}")
					continue

			# Batch update channels for better performance
			if cached_channels:
				operations = [
					UpdateOne(
						{"guild_id": guild.id, "id": ch["id"]},
						{"$set": ch},
						upsert=True
					)
					for ch in cached_channels
				]

				await self.channels.bulk_write(operations, ordered=False)
				logger.debug(f"Cached {len(cached_channels)} channels for {guild.name}")

		except Exception as e:
			logger.error(f"Error caching channels for {guild.name}: {e}")
			raise

	async def cache_roles(self, guild: discord.Guild):
		"""Enhanced role caching with better permission analysis and hierarchy tracking."""
		try:
			cached_roles = []

			for role in guild.roles:
				try:
					# Analyze role permissions for better insights
					dangerous_perms = [
						"administrator", "manage_guild", "manage_roles", "manage_channels",
						"kick_members", "ban_members", "manage_messages", "mention_everyone"
					]

					moderation_perms = [
						"kick_members", "ban_members", "manage_messages", "mute_members",
						"deafen_members", "move_members"
					]

					has_dangerous_perms = any(
						getattr(role.permissions, perm, False) for perm in dangerous_perms
					)

					has_moderation_perms = any(
						getattr(role.permissions, perm, False) for perm in moderation_perms
					)

					role_data = {
						"guild_id": guild.id,
						"id": role.id,
						"name": role.name,
						"color": str(role.color),
						"color_value": role.color.value,
						"permissions": role.permissions.value,
						"position": role.position,
						"mentionable": role.mentionable,
						"hoist": role.hoist,
						"managed": role.managed,
						"is_default": role.is_default(),
						"is_premium_subscriber": role.is_premium_subscriber(),
						"has_dangerous_permissions": has_dangerous_perms,
						"has_moderation_permissions": has_moderation_perms,
						"member_count": len(role.members),
						"created_at": role.created_at.isoformat(),
						"updated_at": pendulum.now("America/Chicago").isoformat(),

						# Additional metadata
						"display_icon": str(role.display_icon) if hasattr(role,
																		  'display_icon') and role.display_icon else None,
						"unicode_emoji": role.unicode_emoji if hasattr(role, 'unicode_emoji') else None,
					}

					cached_roles.append(role_data)

				except Exception as role_error:
					logger.error(f"Error processing role {role.name}: {role_error}")
					continue

			# Batch update roles
			if cached_roles:
				operations = [
					UpdateOne(
						{"guild_id": guild.id, "id": role["id"]},
						{"$set": role},
						upsert=True
					)
					for role in cached_roles
				]

				await self.roles.bulk_write(operations, ordered=False)
				logger.debug(f"Cached {len(cached_roles)} roles for {guild.name}")

		except Exception as e:
			logger.error(f"Error caching roles for {guild.name}: {e}")
			raise

	async def cache_members(self, guild: discord.Guild):
		"""Enhanced member caching with activity tracking and better data."""
		try:
			cached_members = []

			for member in guild.members:
				try:
					# Calculate account age
					account_age = (datetime.now(timezone.utc) - member.created_at).days

					# Check if member has any suspicious indicators
					suspicious_indicators = []
					if account_age < 7:
						suspicious_indicators.append("very_new_account")
					if not member.display_avatar or str(member.display_avatar.url).endswith("avatars/0.png"):
						suspicious_indicators.append("default_avatar")
					if len(member.roles) <= 1:  # Only @everyone role
						suspicious_indicators.append("no_roles")

					# Enhanced member data
					member_data = {
						"guild_id": guild.id,
						"id": member.id,
						"username": member.name,
						"global_name": member.global_name,
						"display_name": member.display_name or member.name,
						"discriminator": member.discriminator,
						"bot": member.bot,
						"system": member.system,
						"joined_at": member.joined_at.isoformat() if member.joined_at else None,
						"premium_since": member.premium_since.isoformat() if member.premium_since else None,
						"roles": [role.id for role in member.roles if not role.is_default()],
						"role_count": len([role for role in member.roles if not role.is_default()]),
						"top_role_id": member.top_role.id if member.top_role else None,
						"top_role_position": member.top_role.position if member.top_role else 0,
						"permissions": member.guild_permissions.value,
						"avatar_url": str(member.display_avatar.url),
						"status": str(member.status) if hasattr(member, 'status') else None,
						"mobile_status": str(member.mobile_status) if hasattr(member, 'mobile_status') else None,
						"desktop_status": str(member.desktop_status) if hasattr(member, 'desktop_status') else None,
						"web_status": str(member.web_status) if hasattr(member, 'web_status') else None,
						"created_at": member.created_at.isoformat(),
						"account_age_days": account_age,
						"suspicious_indicators": suspicious_indicators,
						"updated_at": pendulum.now("America/Chicago").isoformat(),

						# Enhanced metadata
						"is_owner": member.id == guild.owner_id,
						"guild_permissions_value": member.guild_permissions.value,
						"voice_channel_id": member.voice.channel.id if member.voice else None,
					}

					# Add activity information if available
					if hasattr(member, 'activities') and member.activities:
						activities = []
						for activity in member.activities:
							activity_data = {
								"name": activity.name,
								"type": str(activity.type),
							}
							if hasattr(activity, 'state') and activity.state:
								activity_data["state"] = activity.state
							if hasattr(activity, 'details') and activity.details:
								activity_data["details"] = activity.details
							if hasattr(activity, 'start') and activity.start:
								activity_data["start"] = activity.start.isoformat()
							activities.append(activity_data)
						member_data["activities"] = activities
						member_data["activity_count"] = len(activities)

					cached_members.append(member_data)

				except Exception as member_error:
					logger.error(f"Error processing member {member.name}: {member_error}")
					continue

			# Batch update members with chunking for large guilds
			if cached_members:
				chunk_size = 1000  # Process in chunks to avoid memory issues
				for i in range(0, len(cached_members), chunk_size):
					chunk = cached_members[i:i + chunk_size]
					operations = [
						UpdateOne(
							{"guild_id": guild.id, "id": member["id"]},
							{"$set": member},
							upsert=True
						)
						for member in chunk
					]

					await self.members.bulk_write(operations, ordered=False)

				logger.debug(f"Cached {len(cached_members)} members for {guild.name}")

		except Exception as e:
			logger.error(f"Error caching members for {guild.name}: {e}")
			raise

	async def cache_guild_analytics(self, guild: discord.Guild):
		"""Cache comprehensive guild analytics data"""
		try:
			now = pendulum.now("America/Chicago")
			today = now.format('YYYY-MM-DD')

			# Calculate various metrics
			bot_count = sum(1 for member in guild.members if member.bot)
			human_count = guild.member_count - bot_count
			online_count = sum(1 for member in guild.members
							   if hasattr(member, 'status') and member.status != discord.Status.offline)

			# Role distribution
			role_distribution = defaultdict(int)
			for member in guild.members:
				for role in member.roles:
					if not role.is_default():
						role_distribution[role.name] += 1

			# Channel activity (simplified - would need message tracking for real activity)
			voice_activity = {}
			for vc in guild.voice_channels:
				voice_activity[vc.name] = len(vc.members)

			# Account age distribution
			age_distribution = {'0-7': 0, '8-30': 0, '31-90': 0, '91+': 0}
			for member in guild.members:
				if member.bot:
					continue
				age = (datetime.now(timezone.utc) - member.created_at).days
				if age <= 7:
					age_distribution['0-7'] += 1
				elif age <= 30:
					age_distribution['8-30'] += 1
				elif age <= 90:
					age_distribution['31-90'] += 1
				else:
					age_distribution['91+'] += 1

			analytics_data = {
				"guild_id": guild.id,
				"date": today,
				"timestamp": now.isoformat(),
				"member_stats": {
					"total": guild.member_count,
					"humans": human_count,
					"bots": bot_count,
					"online": online_count,
					"premium": sum(1 for member in guild.members if member.premium_since)
				},
				"channel_stats": {
					"total": len(guild.channels),
					"text": len(guild.text_channels),
					"voice": len(guild.voice_channels),
					"categories": len(guild.categories),
					"voice_active": sum(1 for vc in guild.voice_channels if vc.members)
				},
				"role_stats": {
					"total": len(guild.roles),
					"with_permissions": len([r for r in guild.roles if r.permissions.value > 0]),
					"managed": len([r for r in guild.roles if r.managed]),
					"distribution": dict(role_distribution)
				},
				"voice_activity": voice_activity,
				"age_distribution": age_distribution,
				"guild_features": list(guild.features) if guild.features else [],
				"verification_level": str(guild.verification_level),
				"premium_tier": guild.premium_tier
			}

			await self.analytics.update_one(
				{"guild_id": guild.id, "date": today},
				{"$set": analytics_data},
				upsert=True
			)

			logger.debug(f"Cached analytics for {guild.name} on {today}")

		except Exception as e:
			logger.error(f"Error caching guild analytics for {guild.name}: {e}")

	async def log_guild_event(self, guild_id: int, event_type: str, event_data: Dict):
		"""Log guild events for tracking and analytics"""
		try:
			event_record = {
				"guild_id": guild_id,
				"event_type": event_type,
				"timestamp": pendulum.now("America/Chicago").isoformat(),
				"data": event_data
			}

			await self.events.insert_one(event_record)

			# Keep recent events in memory cache
			self._memory_cache['recent_events'][guild_id].append(event_record)

			# Keep only last 100 events in memory
			if len(self._memory_cache['recent_events'][guild_id]) > 100:
				self._memory_cache['recent_events'][guild_id] = self._memory_cache['recent_events'][guild_id][-100:]

			logger.debug(f"Logged event {event_type} for guild {guild_id}")

		except Exception as e:
			logger.error(f"Error logging guild event: {e}")

	async def get_guild_activity_summary(self, guild_id: int, days: int = 7) -> Dict:
		"""Get activity summary for a guild over specified days"""
		try:
			end_date = pendulum.now("America/Chicago")
			start_date = end_date.subtract(days=days)

			# Get events in date range
			events_cursor = self.events.find({
				"guild_id": guild_id,
				"timestamp": {
					"$gte": start_date.isoformat(),
					"$lte": end_date.isoformat()
				}
			})

			events = await events_cursor.to_list(length=None)

			# Aggregate event data
			event_counts = defaultdict(int)
			for event in events:
				event_counts[event['event_type']] += 1

			return {
				"guild_id": guild_id,
				"period_days": days,
				"total_events": len(events),
				"event_breakdown": dict(event_counts),
				"start_date": start_date.isoformat(),
				"end_date": end_date.isoformat()
			}

		except Exception as e:
			logger.error(f"Error getting guild activity summary: {e}")
			return {}

	async def delete_guild(self, guild_id: int):
		"""Enhanced guild deletion with better logging and cleanup."""
		self._ensure_initialized()

		async with self._get_guild_lock(guild_id):
			try:
				logger.info(f"Starting deletion of cached data for guild {guild_id}")

				# Get counts before deletion for logging
				server_count = await self.servers.count_documents({"id": guild_id})
				channel_count = await self.channels.count_documents({"guild_id": guild_id})
				role_count = await self.roles.count_documents({"guild_id": guild_id})
				member_count = await self.members.count_documents({"guild_id": guild_id})
				analytics_count = await self.analytics.count_documents({"guild_id": guild_id})
				events_count = await self.events.count_documents({"guild_id": guild_id})

				# Perform deletions concurrently
				results = await asyncio.gather(
					self.servers.delete_many({"id": guild_id}),
					self.channels.delete_many({"guild_id": guild_id}),
					self.roles.delete_many({"guild_id": guild_id}),
					self.members.delete_many({"guild_id": guild_id}),
					self.analytics.delete_many({"guild_id": guild_id}),
					self.events.delete_many({"guild_id": guild_id}),
					return_exceptions=True
				)

				logger.info(
					f"Deleted cached data for guild {guild_id}: "
					f"{server_count} servers, {channel_count} channels, "
					f"{role_count} roles, {member_count} members, "
					f"{analytics_count} analytics, {events_count} events"
				)

				# Clean up memory cache
				if guild_id in self._memory_cache['guild_stats']:
					del self._memory_cache['guild_stats'][guild_id]
				if guild_id in self._memory_cache['recent_events']:
					del self._memory_cache['recent_events'][guild_id]

				# Clean up the lock
				if guild_id in self._cache_locks:
					del self._cache_locks[guild_id]

			except Exception as e:
				logger.error(f"Error deleting guild cache for {guild_id}: {e}")
				raise

	# Enhanced utility methods

	async def get_cached_guild_info(self, guild_id: int) -> Optional[Dict]:
		"""Retrieve cached guild information with memory cache fallback."""
		try:
			# Try memory cache first
			if guild_id in self._memory_cache['guild_stats']:
				memory_data = self._memory_cache['guild_stats'][guild_id]
				# If data is less than 5 minutes old, use it
				if (datetime.now(timezone.utc) - memory_data['last_update']).seconds < 300:
					return memory_data

			# Fallback to database
			return await self.servers.find_one({"id": guild_id})
		except Exception as e:
			logger.error(f"Error retrieving cached guild info for {guild_id}: {e}")
			return None

	async def get_cached_channels(self, guild_id: int, channel_type: str = None) -> List[Dict]:
		"""Retrieve cached channels, optionally filtered by type."""
		try:
			query = {"guild_id": guild_id}
			if channel_type:
				query["type"] = channel_type

			cursor = self.channels.find(query).sort("position", 1)
			return await cursor.to_list(length=None)
		except Exception as e:
			logger.error(f"Error retrieving cached channels for {guild_id}: {e}")
			return []

	async def get_cached_member(self, guild_id: int, user_id: int) -> Optional[Dict]:
		"""Retrieve a specific cached member."""
		try:
			return await self.members.find_one({"guild_id": guild_id, "id": user_id})
		except Exception as e:
			logger.error(f"Error retrieving cached member {user_id} for guild {guild_id}: {e}")
			return None

	async def get_guild_statistics(self, guild_id: int) -> Dict:
		"""Get comprehensive statistics about a cached guild."""
		try:
			stats = {
				"total_channels": await self.channels.count_documents({"guild_id": guild_id}),
				"total_roles": await self.roles.count_documents({"guild_id": guild_id}),
				"total_members": await self.members.count_documents({"guild_id": guild_id}),
				"bot_members": await self.members.count_documents({"guild_id": guild_id, "bot": True}),
				"human_members": await self.members.count_documents({"guild_id": guild_id, "bot": False}),
				"suspicious_members": await self.members.count_documents({
					"guild_id": guild_id,
					"suspicious_indicators": {"$exists": True, "$not": {"$size": 0}}
				}),
			}

			# Get channel type breakdown
			channel_types = await self.channels.aggregate([
				{"$match": {"guild_id": guild_id}},
				{"$group": {"_id": "$type", "count": {"$sum": 1}}}
			]).to_list(length=None)

			stats["channel_types"] = {ct["_id"]: ct["count"] for ct in channel_types}

			# Get latest analytics
			latest_analytics = await self.analytics.find_one(
				{"guild_id": guild_id},
				sort=[("timestamp", -1)]
			)

			if latest_analytics:
				stats["latest_analytics"] = latest_analytics
				stats["analytics_date"] = latest_analytics.get("date")

			return stats
		except Exception as e:
			logger.error(f"Error getting guild statistics for {guild_id}: {e}")
			return {}

	async def get_member_insights(self, guild_id: int) -> Dict:
		"""Get detailed member insights for a guild"""
		try:
			pipeline = [
				{"$match": {"guild_id": guild_id}},
				{"$group": {
					"_id": None,
					"total_members": {"$sum": 1},
					"bot_count": {"$sum": {"$cond": ["$bot", 1, 0]}},
					"avg_account_age": {"$avg": "$account_age_days"},
					"new_accounts": {"$sum": {"$cond": [{"$lte": ["$account_age_days", 7]}, 1, 0]}},
					"suspicious_count": {"$sum": {"$cond": [{"$gt": [{"$size": "$suspicious_indicators"}, 0]}, 1, 0]}},
					"premium_members": {"$sum": {"$cond": ["$premium_since", 1, 0]}},
				}}
			]

			result = await self.members.aggregate(pipeline).to_list(length=1)

			if result:
				insights = result[0]
				insights['human_count'] = insights['total_members'] - insights['bot_count']
				return insights

			return {}

		except Exception as e:
			logger.error(f"Error getting member insights for {guild_id}: {e}")
			return {}

	async def cleanup_stale_data(self, max_age_hours: int = 168):  # 1 week default
		"""Clean up stale cached data older than specified hours."""
		try:
			cutoff_time = pendulum.now("America/Chicago").subtract(hours=max_age_hours)
			cutoff_iso = cutoff_time.isoformat()

			# Clean up stale guild data
			stale_guilds = await self.servers.find(
				{"updated_at": {"$lt": cutoff_iso}}
			).to_list(length=None)

			deleted_count = 0
			for guild_data in stale_guilds:
				await self.delete_guild(guild_data["id"])
				deleted_count += 1

			# Clean up old events (keep last 30 days)
			old_events_cutoff = pendulum.now("America/Chicago").subtract(days=30).isoformat()
			events_deleted = await self.events.delete_many({
				"timestamp": {"$lt": old_events_cutoff}
			})

			# Clean up old analytics (keep last 90 days)
			old_analytics_cutoff = pendulum.now("America/Chicago").subtract(days=90).format('YYYY-MM-DD')
			analytics_deleted = await self.analytics.delete_many({
				"date": {"$lt": old_analytics_cutoff}
			})

			logger.info(f"Cleaned up {deleted_count} stale guild caches, "
						f"{events_deleted.deleted_count} old events, "
						f"{analytics_deleted.deleted_count} old analytics")
			return deleted_count

		except Exception as e:
			logger.error(f"Error during cleanup of stale data: {e}")
			return 0

	async def __aenter__(self):
		"""Async context manager entry."""
		await self.initialize()
		return self

	async def __aexit__(self, exc_type, exc_val, exc_tb):
		"""Async context manager exit."""
		await self.close()


# Factory function to create and initialize cache manager
async def create_cache_manager(mongo_uri: str) -> GuildCacheManager:
	"""
	Factory function to create and initialize a GuildCacheManager.

	Args:
		mongo_uri: MongoDB connection URI for the cache database

	Returns:
		Initialized GuildCacheManager instance
	"""
	cache_manager = GuildCacheManager(mongo_uri)
	await cache_manager.initialize()
	return cache_manager


# Global cache manager instance (will be initialized in sync.py)
cache_manager: Optional[GuildCacheManager] = None