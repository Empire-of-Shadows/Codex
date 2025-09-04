# python
import os
import asyncio
from datetime import datetime, timezone
from typing import Dict, Optional, List

import discord
from discord.ext import commands

from utils.logger import get_logger, PerformanceLogger, log_context
from Database.DatabaseManager import db_manager
from dotenv import load_dotenv

load_dotenv()

logger = get_logger("UpdatesDrops.DropsStatsCog")


class DropsStatsCog(commands.Cog):
	"""
    Discord Cog that:
      - Initializes MongoDB database/collections on load
      - Listens for posts in specific channels
      - Tracks monthly counts per channel and a running average-per-month
    """

	def __init__(self, bot: commands.Bot):
		self.bot = bot

		# Discord channel -> logical collection name mapping
		self.channel_map: Dict[int, str] = {
			1314381131768008757: "Updates",  # Updates
			1317187274496016494: "Free",  # Free games
			1316889374318923856: "Prime",  # Prime drops
		}
		logger.debug(f"Initialized channel map with {len(self.channel_map)} entries: {list(self.channel_map.items())}")

		# Simple lock to serialize writes if desired (Mongo ops are atomic, but this can reduce interleaving)
		self._op_lock = asyncio.Lock()

		logger.info("DropsStatsCog created using DatabaseManager")

	async def cog_load(self):
		"""Run when the cog is loaded: initialize DatabaseManager."""
		logger.info("Loading DropsStatsCog...")
		with PerformanceLogger(logger, "drops_stats_cog_load"):
			await self._initialize_database()
		logger.info("DropsStatsCog loaded successfully")

	def cog_unload(self):
		"""Run when the cog is unloaded: DatabaseManager handles cleanup automatically."""
		logger.info("Unloading DropsStatsCog...")
		logger.info("DatabaseManager will handle connection cleanup")

	async def _initialize_database(self):
		"""Initialize DatabaseManager."""
		with PerformanceLogger(logger, "drops_stats_db_init"):
			try:
				# Initialize the global database manager
				await db_manager.initialize()

				# Test connectivity by checking collection health
				monthly_manager = db_manager.get_collection_manager('updates_monthly')
				totals_manager = db_manager.get_collection_manager('updates_totals')

				# Simple connectivity test
				await monthly_manager.count_documents({})
				await totals_manager.count_documents({})

				logger.info("DatabaseManager initialized successfully for DropsStatsCog")
			except Exception as e:
				logger.error("Database initialization failed: %s", e, exc_info=True)
				raise

	# ---------------------------
	# Helpers
	# ---------------------------
	@staticmethod
	def _normalize_embed_list(embeds: List[discord.Embed]) -> List[dict]:
		"""
        Convert embed objects to plain dicts for stable comparison.
        """
		try:
			return [e.to_dict() for e in embeds or []]
		except Exception:
			# Fallback: basic fields if to_dict is unavailable for any reason
			norm = []
			for e in embeds or []:
				norm.append({
					"title": getattr(e, "title", None),
					"description": getattr(e, "description", None),
					"color": getattr(e.color, "value", None) if getattr(e, "color", None) else None,
					"footer": getattr(getattr(e, "footer", None), "text", None),
					"thumbnail": getattr(getattr(e, "thumbnail", None), "url", None),
					"image": getattr(getattr(e, "image", None), "url", None),
					"author": getattr(getattr(e, "author", None), "name", None),
					"fields": [
						{"name": f.name, "value": f.value, "inline": f.inline}
						for f in getattr(e, "fields", []) or []
					],
				})
			logger.debug("Embeds normalized via fallback path; count=%d", len(norm))
			return norm

	@classmethod
	def _embeds_changed(cls, before: List[discord.Embed], after: List[discord.Embed]) -> bool:
		"""
        Determine if embeds changed meaningfully between before and after.
        """
		b = cls._normalize_embed_list(before)
		a = cls._normalize_embed_list(after)
		changed = b != a
		logger.debug("Embeds changed=%s (before_count=%d, after_count=%d)", changed, len(b), len(a))
		return changed

	# ---------------------------
	# Event listeners
	# ---------------------------
	@commands.Cog.listener("on_message")
	async def handle_message(self, message: discord.Message):
		"""
        Listen for posts in tracked channels.
        Count all messages in tracked channels (including embeds posted by bots/webhooks).
        """
		# Ignore DMs
		if message.guild is None:
			logger.debug("on_message ignored: message %s is from DM", getattr(message, "id", "unknown"))
			return

		coll_name = self.channel_map.get(message.channel.id)
		if not coll_name:
			logger.debug(
				"on_message ignored: channel %s (#%s) not in channel_map",
				message.channel.id, getattr(message.channel, "name", "unknown")
			)
			return

		# Detect webhook messages
		is_webhook = message.webhook_id is not None

		event_dt = message.created_at
		if event_dt is None:
			logger.debug("Message %s has no created_at; using now()", message.id)
			event_dt = datetime.now(tz=timezone.utc)
		elif event_dt.tzinfo is None:
			logger.debug("Message %s created_at naive; setting tz=UTC", message.id)
			event_dt = event_dt.replace(tzinfo=timezone.utc)

		with log_context(logger, "drops_message_process"):
			logger.debug(
				"Processing message %s in #%s (%s) mapped to '%s' at %s (author_id=%s, author_bot=%s, is_webhook=%s, webhook_id=%s, embeds=%d, content_len=%s)",
				message.id, getattr(message.channel, "name", "unknown"), message.channel.id,
				coll_name, event_dt.isoformat(), getattr(message.author, "id", None),
				getattr(message.author, "bot", None),
				is_webhook, getattr(message, "webhook_id", None),
				len(message.embeds or []),
				len(getattr(message, "content", "") or "")
			)

			# Perform DB updates
			logger.debug("Attempting to acquire operation lock for message %s...", message.id)
			async with self._op_lock:
				logger.debug("Operation lock acquired for message %s", message.id)
				try:
					await self._process_event_async(coll_name, event_dt)
					logger.debug("Message %s processed successfully for '%s'", message.id, coll_name)
				except Exception as e:
					logger.error(
						"Failed processing message %s in channel %s: %s",
						message.id, message.channel.id, e, exc_info=True
					)
				finally:
					logger.debug("Releasing operation lock for message %s", message.id)

	@commands.Cog.listener("on_message_edit")
	async def handle_message_edit(self, before: discord.Message, after: discord.Message):
		"""
        Listen for edits in tracked channels.
        Increment when embeds were added or changed on an existing message.
        """
		# Ignore DMs
		if after.guild is None:
			logger.debug("on_message_edit ignored: message %s is from DM", getattr(after, "id", "unknown"))
			return

		coll_name = self.channel_map.get(after.channel.id)
		if not coll_name:
			logger.debug(
				"on_message_edit ignored: channel %s (#%s) not in channel_map",
				after.channel.id, getattr(after.channel, "name", "unknown")
			)
			return

		# Only count when embeds changed meaningfully (added/modified/removed->added)
		if not self._embeds_changed(before.embeds or [], after.embeds or []):
			logger.debug("on_message_edit ignored: no meaningful embed change for message %s", after.id)
			return

		# Detect webhook edits
		is_webhook = after.webhook_id is not None

		event_dt = after.edited_at or after.created_at or datetime.now(tz=timezone.utc)
		if event_dt.tzinfo is None:
			logger.debug("Edit event datetime naive; setting tz=UTC for message %s", after.id)
			event_dt = event_dt.replace(tzinfo=timezone.utc)

		with log_context(logger, "drops_message_edit_process"):
			logger.debug(
				"Processing message edit %s in #%s (%s) mapped to '%s' at %s (is_webhook=%s, webhook_id=%s, embeds_before=%d, embeds_after=%d)",
				after.id, getattr(after.channel, "name", "unknown"), after.channel.id,
				coll_name, event_dt.isoformat(),
				is_webhook, getattr(after, "webhook_id", None),
				len(before.embeds or []), len(after.embeds or [])
			)

			logger.debug("Attempting to acquire operation lock for edit %s...", after.id)
			async with self._op_lock:
				logger.debug("Operation lock acquired for edit %s", after.id)
				try:
					await self._process_event_async(coll_name, event_dt)
					logger.debug("Edit for message %s processed successfully for '%s'", after.id, coll_name)
				except Exception as e:
					logger.error(
						"Failed processing edit for message %s in channel %s: %s",
						after.id, after.channel.id, e, exc_info=True
					)
				finally:
					logger.debug("Releasing operation lock for edit %s", after.id)

	# ---------------------------
	# Async DB logic using DatabaseManager
	# ---------------------------
	async def _process_event_async(self, coll_name: str, event_dt: datetime) -> None:
		"""
        For each message event:
          - Upsert monthly count doc and increment count.
          - If this is the first message for the month (doc was created), increment months_with_data.
          - Increment total_count.
          - Recompute and store average_per_month (rounded to 2 decimals).
        """
		try:
			logger.debug(
				"Begin _process_event_async for coll='%s', event_dt='%s'",
				coll_name, event_dt.isoformat()
			)
			year = event_dt.year
			month = event_dt.month
			now = datetime.now(tz=timezone.utc)

			# Get collection managers
			monthly_manager = db_manager.get_collection_manager('updates_monthly')
			totals_manager = db_manager.get_collection_manager('updates_totals')

			monthly_id = {"coll": coll_name, "year": year, "month": month}
			logger.debug("Monthly doc _id=%s", monthly_id)

			with PerformanceLogger(logger, f"monthly_increment::{coll_name}::{year}-{month:02d}"):
				# Check if document exists before update to determine if it's a new month
				existing_monthly = await monthly_manager.find_one({"_id": monthly_id})
				new_month_started = existing_monthly is None

				# Upsert monthly document
				await monthly_manager.update_one(
					{"_id": monthly_id},
					{
						"$inc": {"count": 1},
						"$setOnInsert": {"first_event_at": now},
						"$set": {"updated_at": now},
					},
					upsert=True
				)

			logger.debug(
				"Monthly stats update completed for %s (new_month=%s)",
				monthly_id, new_month_started
			)

			# Build totals update
			totals_inc = {"total_count": 1}
			if new_month_started:
				totals_inc["months_with_data"] = 1
			logger.debug("Totals increment payload: %s", totals_inc)

			with PerformanceLogger(logger, f"totals_update::{coll_name}"):
				await totals_manager.update_one(
					{"_id": coll_name},
					{
						"$inc": totals_inc,
						"$set": {"updated_at": now},
					},
					upsert=True
				)

			logger.debug("Totals update completed for '%s'", coll_name)

			# Fetch totals document to compute average
			logger.debug("Fetching totals document for '%s' to compute average...", coll_name)
			totals_doc = await totals_manager.find_one(
				{"_id": coll_name},
				projection={"total_count": 1, "months_with_data": 1}
			)
			logger.debug("Totals doc fetched: %s", totals_doc)

			if totals_doc:
				total = int(totals_doc.get("total_count", 0))
				months = int(totals_doc.get("months_with_data", 0))
				avg = round((total / months), 2) if months > 0 else 0.0
				logger.debug("Computed average_per_month=%.2f from total=%d and months=%d", avg, total, months)

				await totals_manager.update_one(
					{"_id": coll_name},
					{"$set": {"average_per_month": avg, "updated_at": now}}
				)

				logger.debug(
					"Totals updated for %s: total=%d, months=%d, avg=%.2f",
					coll_name, total, months, avg
				)
			else:
				logger.warning("Totals document missing for %s after update", coll_name)

			logger.debug("End _process_event_async for coll='%s' %04d-%02d", coll_name, year, month)

		except Exception as e:
			logger.error(
				"Error while processing event for '%s' (%04d-%02d): %s",
				coll_name, event_dt.year, event_dt.month, e, exc_info=True
			)
			raise


async def setup(bot: commands.Bot):
	"""Entrypoint for discord.ext.commands cogs."""
	logger.info("Setting up DropsStatsCog via setup()")
	await bot.add_cog(DropsStatsCog(bot))
	logger.info("DropsStatsCog added to bot")