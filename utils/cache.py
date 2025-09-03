import os
import asyncio
from typing import List, Dict, Optional, Set
from datetime import datetime, timezone
import discord
import pendulum
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase, AsyncIOMotorCollection
import logging
from pymongo import UpdateOne

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
		self._cache_locks = {}  # Per-guild locks for thread safety
		self._initialized = False

	async def initialize(self):
		"""Initialize the database connection and collections."""
		if self._initialized:
			return

		try:
			self._client = AsyncIOMotorClient(self.mongo_uri)
			self._db = self._client["Server_Data"]

			# Initialize collections
			self._channels = self._db["servers.channels"]
			self._servers = self._db["servers.guilds"]
			self._members = self._db["servers.members"]
			self._roles = self._db["servers.roles"]

			# Test the connection
			await self._client.admin.command('ping')

			self._initialized = True
			logger.info("GuildCacheManager database connection initialized successfully")

		except Exception as e:
			logger.error(f"Failed to initialize GuildCacheManager database connection: {e}")
			raise

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
		"""Enhanced guild info caching with additional metadata."""
		try:
			# Get additional guild features and settings
			features = list(guild.features) if guild.features else []

			data = {
				"id": guild.id,
				"name": guild.name,
				"icon_url": str(guild.icon.url) if guild.icon else None,
				"banner_url": str(guild.banner.url) if guild.banner else None,
				"description": guild.description,
				"owner_id": guild.owner_id,
				"member_count": guild.member_count,
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
				"cache_version": "2.0"  # For future cache migrations
			}

			await self.servers.update_one(
				{"id": guild.id},
				{"$set": data},
				upsert=True
			)
			logger.debug(f"Cached guild info for {guild.name}")

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
						})

						# Cache active threads if any
						if hasattr(channel, 'threads'):
							threads = []
							async for thread in channel.archived_threads(limit=50):
								threads.append({
									"id": thread.id,
									"name": thread.name,
									"archived": thread.archived,
									"locked": thread.locked,
									"created_at": thread.created_at.isoformat()
								})
							channel_data["archived_threads"] = threads

					# Add voice channel specific data
					elif isinstance(channel, discord.VoiceChannel):
						channel_data.update({
							"bitrate": channel.bitrate,
							"user_limit": channel.user_limit,
							"rtc_region": str(channel.rtc_region) if channel.rtc_region else None,
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
		"""Enhanced role caching with better permission analysis."""
		try:
			cached_roles = []

			for role in guild.roles:
				try:
					# Analyze role permissions for better insights
					dangerous_perms = [
						"administrator", "manage_guild", "manage_roles", "manage_channels",
						"kick_members", "ban_members", "manage_messages", "mention_everyone"
					]

					has_dangerous_perms = any(
						getattr(role.permissions, perm, False) for perm in dangerous_perms
					)

					role_data = {
						"guild_id": guild.id,
						"id": role.id,
						"name": role.name,
						"color": str(role.color),
						"permissions": role.permissions.value,
						"position": role.position,
						"mentionable": role.mentionable,
						"hoist": role.hoist,
						"managed": role.managed,
						"is_default": role.is_default(),
						"is_premium_subscriber": role.is_premium_subscriber(),
						"has_dangerous_permissions": has_dangerous_perms,
						"member_count": len(role.members),
						"created_at": role.created_at.isoformat(),
						"updated_at": pendulum.now("America/Chicago").isoformat(),
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
						"top_role_id": member.top_role.id if member.top_role else None,
						"permissions": member.guild_permissions.value,
						"avatar_url": str(member.display_avatar.url),
						"status": str(member.status) if hasattr(member, 'status') else None,
						"mobile_status": str(member.mobile_status) if hasattr(member, 'mobile_status') else None,
						"desktop_status": str(member.desktop_status) if hasattr(member, 'desktop_status') else None,
						"web_status": str(member.web_status) if hasattr(member, 'web_status') else None,
						"created_at": member.created_at.isoformat(),
						"updated_at": pendulum.now("America/Chicago").isoformat(),
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
							activities.append(activity_data)
						member_data["activities"] = activities

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

				# Perform deletions concurrently
				results = await asyncio.gather(
					self.servers.delete_many({"id": guild_id}),
					self.channels.delete_many({"guild_id": guild_id}),
					self.roles.delete_many({"guild_id": guild_id}),
					self.members.delete_many({"guild_id": guild_id}),
					return_exceptions=True
				)

				logger.info(
					f"Deleted cached data for guild {guild_id}: "
					f"{server_count} servers, {channel_count} channels, "
					f"{role_count} roles, {member_count} members"
				)

				# Clean up the lock
				if guild_id in self._cache_locks:
					del self._cache_locks[guild_id]

			except Exception as e:
				logger.error(f"Error deleting guild cache for {guild_id}: {e}")
				raise

	# New utility methods for enhanced functionality

	async def get_cached_guild_info(self, guild_id: int) -> Optional[Dict]:
		"""Retrieve cached guild information."""
		try:
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
			}

			# Get channel type breakdown
			channel_types = await self.channels.aggregate([
				{"$match": {"guild_id": guild_id}},
				{"$group": {"_id": "$type", "count": {"$sum": 1}}}
			]).to_list(length=None)

			stats["channel_types"] = {ct["_id"]: ct["count"] for ct in channel_types}

			return stats
		except Exception as e:
			logger.error(f"Error getting guild statistics for {guild_id}: {e}")
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

			logger.info(f"Cleaned up {deleted_count} stale guild caches")
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