# python
# python
import os
import asyncio
from datetime import datetime, timezone
from typing import Dict, Optional, List

import discord
from discord.ext import commands

from pymongo import MongoClient
from pymongo.errors import PyMongoError

from utils.logger import get_logger, PerformanceLogger, log_context
from dotenv import load_dotenv

load_dotenv()
mongo_uri = os.getenv("MONGO_URI3")

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
            1314381131768008757: "Updates",      # Updates
            1317187274496016494: "Free",         # Free games
            1316889374318923856: "Prime",        # Prime drops
        }
        logger.debug(f"Initialized channel map with {len(self.channel_map)} entries: {list(self.channel_map.items())}")

        # Mongo connection fields
        self._client: Optional[MongoClient] = None
        self._db = None
        self._stats_monthly = None
        self._stats_totals = None

        # Config: prefer env var; expected to be set by your environment
        self.mongo_uri = mongo_uri
        self.database_name = os.getenv("DROPS_DB_NAME", "Updates-Drops")

        # Collections to store stats
        self.stats_monthly_collection = os.getenv("DROPS_STATS_MONTHLY_COLL", "stats_monthly")
        self.stats_totals_collection = os.getenv("DROPS_STATS_TOTALS_COLL", "stats_totals")

        # Simple lock to serialize writes if desired (Mongo ops are atomic, but this can reduce interleaving)
        self._op_lock = asyncio.Lock()

        logger.info(
            "DropsStatsCog created with DB='%s', monthly_coll='%s', totals_coll='%s'",
            self.database_name, self.stats_monthly_collection, self.stats_totals_collection
        )
        if not self.mongo_uri:
            logger.warning("MONGO_URI3 is not set at Cog init. Database operations will fail until configured.")
        else:
            logger.debug("Mongo URI present (hidden) and will be used to connect during cog load.")

    async def cog_load(self):
        """Run when the cog is loaded: initialize Mongo and test connectivity."""
        logger.info("Loading DropsStatsCog...")
        with PerformanceLogger(logger, "drops_stats_cog_load"):
            await self._initialize_database()
        logger.info("DropsStatsCog loaded successfully")

    def cog_unload(self):
        """Run when the cog is unloaded: close Mongo client."""
        logger.info("Unloading DropsStatsCog...")
        try:
            if self._client:
                self._client.close()
                logger.info("Closed MongoDB client for DropsStatsCog")
        except Exception as e:
            logger.error("Error while closing MongoDB client: %s", e, exc_info=True)
        finally:
            self._client = None

    async def _initialize_database(self):
        """Initialize MongoDB client, DB, and collections."""
        if not self.mongo_uri:
            logger.error("MONGO_URI3 is not set. Unable to initialize DropsStatsCog database connection.")
            raise RuntimeError("MONGO_URI3 is not set. Unable to initialize DropsStatsCog database connection.")

        with PerformanceLogger(logger, "drops_stats_db_init"):
            # Create sync client (we'll use asyncio.to_thread for calls to avoid blocking)
            logger.debug("Creating MongoClient and attaching DB/collections...")
            self._client = MongoClient(self.mongo_uri, appname="drops-stats-cog")
            self._db = self._client[self.database_name]
            self._stats_monthly = self._db[self.stats_monthly_collection]
            self._stats_totals = self._db[self.stats_totals_collection]

            logger.debug(
                "Attached DB handles: db='%s', monthly='%s', totals='%s'",
                self.database_name, self.stats_monthly_collection, self.stats_totals_collection
            )

            # _id fields are automatically and uniquely indexed by MongoDB.
            # We don't need to (and must not) recreate or mark them unique explicitly.
            def _post_init_check():
                try:
                    # Basic ping to confirm connectivity
                    self._client.admin.command("ping")
                except Exception as e:
                    raise RuntimeError(f"Failed MongoDB connectivity check: {e}") from e

            try:
                await asyncio.to_thread(_post_init_check)
                logger.info("MongoDB connectivity verified for database '%s'", self.database_name)
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

            # Perform DB updates in a thread to avoid blocking the event loop
            logger.debug("Attempting to acquire operation lock for message %s...", message.id)
            async with self._op_lock:
                logger.debug("Operation lock acquired for message %s", message.id)
                try:
                    await asyncio.to_thread(self._process_event_sync, coll_name, event_dt)
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
                    await asyncio.to_thread(self._process_event_sync, coll_name, event_dt)
                    logger.debug("Edit for message %s processed successfully for '%s'", after.id, coll_name)
                except Exception as e:
                    logger.error(
                        "Failed processing edit for message %s in channel %s: %s",
                        after.id, after.channel.id, e, exc_info=True
                    )
                finally:
                    logger.debug("Releasing operation lock for edit %s", after.id)
    # ---------------------------
    # Sync DB logic (run in thread)
    # ---------------------------
    def _process_event_sync(self, coll_name: str, event_dt: datetime) -> None:
        """
        For each message event:
          - Upsert monthly count doc and increment count.
          - If this is the first message for the month (doc was created), increment months_with_data.
          - Increment total_count.
          - Recompute and store average_per_month (rounded to 2 decimals).
        """
        try:
            logger.debug(
                "Begin _process_event_sync for coll='%s', event_dt='%s'",
                coll_name, event_dt.isoformat()
            )
            year = event_dt.year
            month = event_dt.month
            now = datetime.now(tz=timezone.utc)

            monthly_id = {"coll": coll_name, "year": year, "month": month}
            logger.debug("Monthly doc _id=%s", monthly_id)

            with PerformanceLogger(logger, f"monthly_increment::{coll_name}::{year}-{month:02d}"):
                update_result = self._stats_monthly.update_one(
                    {"_id": monthly_id},
                    {
                        "$inc": {"count": 1},
                        "$setOnInsert": {"first_event_at": now},
                        "$set": {"updated_at": now},
                    },
                    upsert=True,
                )
            new_month_started = update_result.upserted_id is not None
            logger.debug(
                "Monthly stats update result: matched=%d, modified=%d, upserted_id=%s (new_month=%s)",
                getattr(update_result, "matched_count", -1),
                getattr(update_result, "modified_count", -1),
                str(getattr(update_result, "upserted_id", None)),
                new_month_started
            )

            # Build totals update (avoid conflicts: do not $setOnInsert fields that are also $inc)
            totals_inc = {"total_count": 1}
            if new_month_started:
                totals_inc["months_with_data"] = 1
            logger.debug("Totals increment payload: %s", totals_inc)

            with PerformanceLogger(logger, f"totals_update::{coll_name}"):
                totals_update_result = self._stats_totals.update_one(
                    {"_id": coll_name},
                    {
                        # No $setOnInsert for total_count/months_with_data to prevent conflict with $inc
                        "$inc": totals_inc,
                        "$set": {"updated_at": now},
                    },
                    upsert=True,
                )
            logger.debug(
                "Totals update result: matched=%d, modified=%d, upserted_id=%s",
                getattr(totals_update_result, "matched_count", -1),
                getattr(totals_update_result, "modified_count", -1),
                str(getattr(totals_update_result, "upserted_id", None))
            )

            logger.debug("Fetching totals document for '%s' to compute average...", coll_name)
            totals_doc = self._stats_totals.find_one(
                {"_id": coll_name}, projection={"total_count": 1, "months_with_data": 1}
            )
            logger.debug("Totals doc fetched: %s", totals_doc)

            if totals_doc:
                total = int(totals_doc.get("total_count", 0))
                months = int(totals_doc.get("months_with_data", 0))
                avg = round((total / months), 2) if months > 0 else 0.0
                logger.debug("Computed average_per_month=%.2f from total=%d and months=%d", avg, total, months)

                avg_update_result = self._stats_totals.update_one(
                    {"_id": coll_name},
                    {"$set": {"average_per_month": avg, "updated_at": now}},
                )
                logger.debug(
                    "Average update result: matched=%d, modified=%d",
                    getattr(avg_update_result, "matched_count", -1),
                    getattr(avg_update_result, "modified_count", -1)
                )

                logger.debug(
                    "Totals updated for %s: total=%d, months=%d, avg=%.2f",
                    coll_name, total, months, avg
                )
            else:
                logger.warning("Totals document missing for %s after update", coll_name)

            logger.debug("End _process_event_sync for coll='%s' %04d-%02d", coll_name, year, month)

        except PyMongoError as e:
            logger.error(
                "Mongo error while processing event for '%s' (%04d-%02d): %s",
                coll_name, event_dt.year, event_dt.month, e, exc_info=True
            )
            # Re-raise to surface in callers if needed for diagnostics
            raise
        except Exception as e:
            logger.error(
                "Unexpected error while processing event for '%s' (%04d-%02d): %s",
                coll_name, event_dt.year, event_dt.month, e, exc_info=True
            )
            raise


async def setup(bot: commands.Bot):
    """Entrypoint for discord.ext.commands cogs."""
    logger.info("Setting up DropsStatsCog via setup()")
    await bot.add_cog(DropsStatsCog(bot))
    logger.info("DropsStatsCog added to bot")