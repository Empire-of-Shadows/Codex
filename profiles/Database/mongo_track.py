import asyncio
import os
import time
from collections import defaultdict
from typing import Dict, List, Tuple, Any, Set

from pymongo import UpdateOne
from dotenv import load_dotenv
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient

from utils.bot import s
from utils.logger import get_logger, PerformanceLogger

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI2")

logger = get_logger("mongo_track")


def _utc_ts() -> float:
	return datetime.now(timezone.utc).timestamp()


class TrackManager:
	"""
    Cache-first tracker for guild activity.
    - Collects message, reaction, and voice aggregates in memory
    - Periodically flushes aggregated deltas to MongoDB using bulk writes
    - Minimizes DB round-trips via batching and update operators ($inc, $set, $max, $setOnInsert)
    """

	def __init__(self, flush_interval: int = 60, bulk_batch_size: int = 1000):
		logger.info(f"{s}Initializing TrackManager with flush_interval={flush_interval}s, batch_size={bulk_batch_size}")

		# Caches (nested dicts default)
		self.message_cache = defaultdict(lambda: defaultdict(int))  # {guild_id: {user_id: msg_count}}
		self.voice_cache = defaultdict(lambda: defaultdict(float))  # {guild_id: {user_id: voice_seconds}}
		self.message_length_cache = defaultdict(lambda: defaultdict(int))  # {guild_id: {user_id: longest_msg}}
		self.last_message_time = defaultdict(lambda: defaultdict(float))  # {guild_id: {user_id: timestamp}}
		self.daily_streak_cache = defaultdict(lambda: defaultdict(int))  # {guild_id: {user_id: streak}}
		self.reacted_messages_cache = defaultdict(lambda: defaultdict(int))  # {guild_id: {user_id: reacted_count}}
		self.got_reactions_cache = defaultdict(lambda: defaultdict(int))  # {guild_id: {user_id: count}}
		self.emoji_favorites_cache = defaultdict(
			lambda: defaultdict(lambda: defaultdict(int))
		)  # {guild_id: {user_id: {emoji: count}}}
		self.voice_stats_cache: Dict[str, Dict[str, float | int]] = {}  # {f"{guild}:{user}": metrics}

		# Timing and control
		self.last_flush = time.time()
		self.flush_interval = int(flush_interval)
		self.bulk_batch_size = int(bulk_batch_size)
		self._flush_task: asyncio.Task | None = None
		self._flush_lock = asyncio.Lock()

		# Mongo
		try:
			self.mongo_client = AsyncIOMotorClient(MONGO_URI)
			db = self.mongo_client["Ecom-Server"]
			self.collection = db["Users"]
			logger.info(f"{s}MongoDB connection established successfully")
		except Exception as e:
			logger.error(f"{s}Failed to establish MongoDB connection: {e}")
			raise

		logger.info(
			f"{s}TrackManager initialized successfully (flush_interval={self.flush_interval}s, batch={self.bulk_batch_size})")

	# =========================
	# Public API - Counters
	# =========================
	def increment_message_count(self, guild_id: str, user_id: str, message_length: int, timestamp: float):
		"""Increment message count and track longest message for a user."""
		try:
			logger.debug(f"{s}increment_message_count: guild={guild_id}, user={user_id}, length={message_length}")

			self.message_cache[guild_id][user_id] += 1

			# Longest message
			if message_length > self.message_length_cache[guild_id][user_id]:
				old_length = self.message_length_cache[guild_id][user_id]
				self.message_length_cache[guild_id][user_id] = message_length
				if old_length > 0:  # Only log if it's actually an improvement
					logger.debug(
						f"{s}New longest message for user {user_id}: {message_length} chars (was {old_length})")

			self.last_message_time[guild_id][user_id] = float(timestamp)

		except Exception as e:
			logger.error(f"{s}Error in increment_message_count: guild={guild_id}, user={user_id}, error={e}")

	def increment_voice_time(self, guild_id: str, user_id: str, stats: dict):
		"""
        Update voice statistics with detailed metrics using incremental updates.
        stats keys:
          - voice_seconds
          - active_seconds
          - muted_time
          - deafened_time
          - self_muted_time
          - self_deafened_time
          - active_percentage
          - unmuted_percentage
        """
		try:
			voice_seconds = float(stats.get("voice_seconds", 0) or 0)
			active_seconds = float(stats.get("active_seconds", 0) or 0)

			logger.debug(f"{s}increment_voice_time: guild={guild_id}, user={user_id}, "
						 f"voice_seconds={voice_seconds}, active_seconds={active_seconds}")

			# Update basic voice time
			self.voice_cache[guild_id][user_id] += voice_seconds

			# Create key for detailed stats
			key = f"{guild_id}:{user_id}"
			if key not in self.voice_stats_cache:
				logger.debug(f"{s}Creating new voice stats entry for {key}")
				self.voice_stats_cache[key] = {
					"active_seconds": 0.0,
					"muted_time": 0.0,
					"deafened_time": 0.0,
					"self_muted_time": 0.0,
					"self_deafened_time": 0.0,
					"sessions": 0,
					"total_active_percentage": 0.0,
					"total_unmuted_percentage": 0.0,
				}

			cs = self.voice_stats_cache[key]
			cs["active_seconds"] += active_seconds
			cs["muted_time"] += float(stats.get("muted_time", 0) or 0)
			cs["deafened_time"] += float(stats.get("deafened_time", 0) or 0)
			cs["self_muted_time"] += float(stats.get("self_muted_time", 0) or 0)
			cs["self_deafened_time"] += float(stats.get("self_deafened_time", 0) or 0)
			cs["sessions"] += 1

			# Running averages
			sessions = int(cs["sessions"])
			if sessions > 0:
				ap = float(stats.get("active_percentage", 0) or 0)
				up = float(stats.get("unmuted_percentage", 0) or 0)
				cs["total_active_percentage"] = ((cs["total_active_percentage"] * (sessions - 1)) + ap) / sessions
				cs["total_unmuted_percentage"] = ((cs["total_unmuted_percentage"] * (sessions - 1)) + up) / sessions

			logger.debug(f"{s}Voice stats updated for {key}: sessions={sessions}, "
						 f"active_pct={cs['total_active_percentage']:.2f}%")

		except Exception as e:
			logger.error(f"{s}Error in increment_voice_time: guild={guild_id}, user={user_id}, error={e}")

	def increment_reaction_count(self, guild_id: str, reactor_id: str, message_owner_id: str, emoji: str):
		"""Increment per-user reaction stats and emoji favorites"""
		try:
			logger.debug(f"{s}increment_reaction_count: guild={guild_id}, reactor={reactor_id}, emoji={emoji}")

			self.reacted_messages_cache[guild_id][reactor_id] += 1
			self.emoji_favorites_cache[guild_id][reactor_id][str(emoji)] += 1

		except Exception as e:
			logger.error(f"{s}Error updating reaction count: guild={guild_id}, reactor={reactor_id}, error={e}")

	async def increment_reacted_messages(self, guild_id: str, user_id: str):
		try:
			logger.debug(f"{s}increment_reacted_messages: guild={guild_id}, user={user_id}")
			self.reacted_messages_cache[guild_id][user_id] += 1
		except Exception as e:
			logger.error(f"{s}Error in increment_reacted_messages: guild={guild_id}, user={user_id}, error={e}")

	async def increment_got_reactions(self, guild_id: str, user_id: str):
		try:
			logger.debug(f"{s}increment_got_reactions: guild={guild_id}, user={user_id}")
			self.got_reactions_cache[guild_id][user_id] += 1
		except Exception as e:
			logger.error(f"{s}Error in increment_got_reactions: guild={guild_id}, user={user_id}, error={e}")

	async def get_daily_streak(self, guild_id: str, user_id: str) -> int:
		try:
			streak = int(self.daily_streak_cache[guild_id][user_id])
			logger.debug(f"{s}get_daily_streak: guild={guild_id}, user={user_id}, streak={streak}")
			return streak
		except Exception as e:
			logger.error(f"{s}Error in get_daily_streak: guild={guild_id}, user={user_id}, error={e}")
			return 0

	async def get_longest_message(self, guild_id: str, user_id: str) -> int:
		try:
			length = int(self.message_length_cache[guild_id][user_id])
			logger.debug(f"{s}get_longest_message: guild={guild_id}, user={user_id}, length={length}")
			return length
		except Exception as e:
			logger.error(f"{s}Error in get_longest_message: guild={guild_id}, user={user_id}, error={e}")
			return 0

	# =========================
	# Auto flush service
	# =========================
	def start_auto_flush(self):
		if self._flush_task and not self._flush_task.done():
			logger.warning(f"{s}Auto-flush task already running, skipping start request")
			return

		logger.info(f"{s}Starting auto-flush service...")
		self._flush_task = asyncio.create_task(self._auto_flush())
		logger.info(f"{s}Auto-flush task started successfully")

	def stop_auto_flush(self):
		if self._flush_task:
			logger.info(f"{s}Stopping auto-flush service...")
			self._flush_task.cancel()
			logger.info(f"{s}Auto-flush task cancelled")
		else:
			logger.debug(f"{s}No auto-flush task to stop")

	async def _auto_flush(self):
		logger.info(f"{s}Auto-flush loop starting with interval {self.flush_interval}s")
		flush_count = 0

		try:
			while True:
				await asyncio.sleep(self.flush_interval)
				flush_count += 1

				with PerformanceLogger(logger, f"auto-flush-{flush_count}"):
					await self.flush_to_db()

		except asyncio.CancelledError:
			logger.info(f"{s}Auto-flush loop cancelled after {flush_count} flushes")
			raise
		except Exception as e:
			logger.error(f"{s}Auto-flush loop error after {flush_count} flushes: {e}")
			# Consider whether to restart the loop or let it die
			logger.warning(f"{s}Auto-flush loop terminated unexpectedly")

	# =========================
	# Internal helpers
	# =========================
	async def _fetch_existing_docs(self, guild_id: str, user_ids: Set[str]) -> Dict[str, dict]:
		"""
        Batch-fetch existing docs for users in a guild to avoid N x find_one.
        Returns a mapping user_id -> doc (partial).
        """
		if not user_ids:
			logger.debug(f"{s}_fetch_existing_docs: No user_ids provided")
			return {}

		logger.debug(f"{s}_fetch_existing_docs: Fetching {len(user_ids)} users for guild {guild_id}")

		try:
			with PerformanceLogger(logger, f"fetch-existing-docs-{len(user_ids)}-users"):
				cursor = self.collection.find(
					{"guild_id": guild_id, "user_id": {"$in": list(user_ids)}},
					{
						"user_id": 1,
						"message_stats.longest_message": 1,
						"message_stats.daily_streak": 1,
						"message_stats.streak_timestamp": 1,
					},
				)
				result: Dict[str, dict] = {}
				doc_count = 0
				async for doc in cursor:
					uid = doc.get("user_id")
					if uid is not None:
						result[str(uid)] = doc
						doc_count += 1

				logger.debug(f"{s}_fetch_existing_docs: Retrieved {doc_count} documents for guild {guild_id}")
				return result

		except Exception as e:
			logger.error(f"{s}Error in _fetch_existing_docs: guild={guild_id}, user_count={len(user_ids)}, error={e}")
			return {}

	@staticmethod
	def _merge_default_structure(guild_id: str, user_id: str, updates: dict) -> dict:
		"""
        Merge updates with the default structure, preserving types.
        """
		DEFAULT = {
			"guild_id": guild_id,
			"user_id": user_id,
			"message_stats": {
				"daily_streak": 0,
				"last_message_time": 0.0,
				"longest_message": 0,
				"messages": 0,
				"reacted_messages": 0,
				"got_reactions": 0,
				"streak_timestamp": 0.0,
			},
			"voice_stats": {
				"active_seconds": 0.0,
				"deafened_time": 0.0,
				"muted_time": 0.0,
				"self_deafened_time": 0.0,
				"self_muted_time": 0.0,
				"total_active_percentage": 0.0,
				"total_unmuted_percentage": 0.0,
				"voice_seconds": 0.0,
				"voice_sessions": 0,
			},
			"last_rewarded": {
				"message": 0.0,
				"voice": 0.0,
				"got_reaction": 0.0,
				"give_reaction": 0.0,
			},
			"favorites": {},
			"xp": 0,
			"embers": 0,
			"level": 0,
		}

		def deep_merge(template: dict, patch: dict) -> dict:
			out = template.copy()
			for k, v in patch.items():
				if k in out and isinstance(out[k], dict) and isinstance(v, dict):
					out[k] = deep_merge(out[k], v)
				else:
					out[k] = v
			return out

		return deep_merge(DEFAULT, updates)

	@staticmethod
	def _compute_streak(prev_streak: int, prev_ts: float, now_ts: float) -> Tuple[int, float]:
		"""
        Compute updated streak given previous streak and timestamp.
        """
		if not prev_ts:
			return 1, now_ts
		try:
			current_date = datetime.fromtimestamp(now_ts, tz=timezone.utc).date()
			last_date = datetime.fromtimestamp(prev_ts, tz=timezone.utc).date()
			days_diff = (current_date - last_date).days
			if days_diff == 1:
				return max(0, prev_streak) + 1, now_ts
			if days_diff > 1:
				return 1, now_ts
			return max(0, prev_streak), float(prev_ts)
		except Exception as e:
			logger.warning(
				f"Error computing streak: prev_streak={prev_streak}, prev_ts={prev_ts}, now_ts={now_ts}, error={e}")
			return 1, now_ts

	# =========================
	# Flush to DB
	# =========================
	async def flush_to_db(self):
		"""Main flush method with comprehensive logging and performance tracking."""
		# Prevent concurrent flushes
		async with self._flush_lock:
			start_time = time.time()

			# Log cache sizes before flush
			cache_stats = {
				'messages': sum(len(guild_users) for guild_users in self.message_cache.values()),
				'voice': sum(len(guild_users) for guild_users in self.voice_cache.values()),
				'voice_detailed': len(self.voice_stats_cache),
				'reactions': sum(len(guild_users) for guild_users in self.reacted_messages_cache.values()),
				'emojis': sum(len(guild_users) for guild_users in self.emoji_favorites_cache.values())
			}

			logger.info(f"{s}Starting flush_to_db - Cache stats: {cache_stats}")

			# Build guild and user sets from caches
			all_guilds: Set[str] = (
					set(self.message_cache.keys())
					| set(self.voice_cache.keys())
					| set(self.message_length_cache.keys())
					| set(self.reacted_messages_cache.keys())
					| set(self.got_reactions_cache.keys())
					| {key.split(":")[0] for key in self.voice_stats_cache.keys()}
			)

			if not all_guilds:
				logger.debug(f"{s}No cached data to flush")
				return

			logger.info(f"{s}Processing {len(all_guilds)} guilds for flush")

			now = _utc_ts()
			total_ops = 0
			operations_batch: List[UpdateOne] = []
			guild_stats = {}

			try:
				for guild_id in all_guilds:
					guild_start = time.time()

					users: Set[str] = (
							set(self.message_cache[guild_id].keys())
							| set(self.voice_cache[guild_id].keys())
							| set(self.message_length_cache[guild_id].keys())
							| set(self.reacted_messages_cache[guild_id].keys())
							| set(self.got_reactions_cache[guild_id].keys())
							| {key.split(":")[1] for key in self.voice_stats_cache.keys() if
							   key.startswith(f"{guild_id}:")}
							| set(self.last_message_time[guild_id].keys())
					)

					if not users:
						continue

					logger.debug(f"{s}Processing guild {guild_id} with {len(users)} users")

					# Batch fetch existing docs for streak/longest comparisons
					existing_docs = await self._fetch_existing_docs(guild_id, users)

					user_ops_count = 0
					for user_id in users:
						inc_updates: Dict[str, Any] = {}
						set_updates: Dict[str, Any] = {}
						max_updates: Dict[str, Any] = {}

						# Streak: only compute if user had activity this window
						had_activity = (
								user_id in self.message_cache[guild_id]
								or user_id in self.voice_cache[guild_id]
						)
						if had_activity:
							prev_doc = existing_docs.get(user_id, {})
							prev_ms = (prev_doc.get("message_stats") or {})
							prev_streak = int(prev_ms.get("daily_streak", 0) or 0)
							prev_ts = float(prev_ms.get("streak_timestamp", 0) or 0)
							new_streak, new_ts = self._compute_streak(prev_streak, prev_ts, now)
							set_updates["message_stats.daily_streak"] = new_streak
							set_updates["message_stats.streak_timestamp"] = new_ts
							# Also mirror in cache for quick reads
							self.daily_streak_cache[guild_id][user_id] = new_streak

						# Message counters
						if user_id in self.message_cache[guild_id]:
							msg_count = int(self.message_cache[guild_id][user_id])
							inc_updates["message_stats.messages"] = msg_count
							logger.debug(f"{s}User {user_id} message increment: {msg_count}")

						if user_id in self.last_message_time[guild_id]:
							set_updates["message_stats.last_message_time"] = float(
								self.last_message_time[guild_id][user_id]
							)

						if user_id in self.message_length_cache[guild_id]:
							# Use $max directly to avoid an extra read/compare
							max_updates["message_stats.longest_message"] = int(
								self.message_length_cache[guild_id][user_id]
							)

						if user_id in self.reacted_messages_cache[guild_id]:
							inc_updates["message_stats.reacted_messages"] = int(
								self.reacted_messages_cache[guild_id][user_id]
							)

						if user_id in self.got_reactions_cache[guild_id]:
							inc_updates["message_stats.got_reactions"] = int(
								self.got_reactions_cache[guild_id][user_id]
							)

						# Emoji favorites
						user_emoji_counts = (self.emoji_favorites_cache.get(guild_id, {}) or {}).get(user_id, {})
						if user_emoji_counts:
							emoji_count = sum(user_emoji_counts.values())
							logger.debug(
								f"{s}User {user_id} emoji updates: {len(user_emoji_counts)} types, {emoji_count} total")

						for emoji, count in user_emoji_counts.items():
							inc_updates[f"favorites.{emoji}"] = int(count)

						# Voice stats
						key = f"{guild_id}:{user_id}"
						if key in self.voice_stats_cache:
							vs = self.voice_stats_cache[key]
							voice_seconds = float(vs.get("active_seconds", 0) or 0)

							logger.debug(f"{s}User {user_id} voice update: {voice_seconds}s active, "
										 f"{vs.get('sessions', 0)} sessions")

							inc_updates.update({
								"voice_stats.voice_seconds": voice_seconds,
								"voice_stats.active_seconds": float(vs.get("active_seconds", 0) or 0),
								"voice_stats.muted_time": float(vs.get("muted_time", 0) or 0),
								"voice_stats.deafened_time": float(vs.get("deafened_time", 0) or 0),
								"voice_stats.self_muted_time": float(vs.get("self_muted_time", 0) or 0),
								"voice_stats.self_deafened_time": float(vs.get("self_deafened_time", 0) or 0),
								"voice_stats.voice_sessions": 1,
							})
							# Running averages (set)
							if int(vs.get("sessions", 0) or 0) > 0:
								set_updates.update({
									"voice_stats.total_active_percentage": float(
										vs.get("total_active_percentage", 0) or 0),
									"voice_stats.total_unmuted_percentage": float(
										vs.get("total_unmuted_percentage", 0) or 0),
								})

						# Build full flattened setOnInsert baseline
						set_on_insert_full: Dict[str, Any] = {
							"guild_id": guild_id,
							"user_id": user_id,
							"xp": 0,
							"embers": 0,
							"level": 0,

							# message_stats primitives (flattened)
							"message_stats.daily_streak": 0,
							"message_stats.streak_timestamp": 0.0,
							"message_stats.messages": 0,
							"message_stats.longest_message": 0,
							"message_stats.reacted_messages": 0,
							"message_stats.got_reactions": 0,
							"message_stats.last_message_time": 0.0,

							# voice_stats primitives (flattened)
							"voice_stats.voice_seconds": 0.0,
							"voice_stats.active_seconds": 0.0,
							"voice_stats.muted_time": 0.0,
							"voice_stats.deafened_time": 0.0,
							"voice_stats.self_muted_time": 0.0,
							"voice_stats.self_deafened_time": 0.0,
							"voice_stats.total_active_percentage": 0.0,
							"voice_stats.total_unmuted_percentage": 0.0,
							"voice_stats.voice_sessions": 0,

							# last_rewarded primitives (flattened)
							"last_rewarded.message": 0.0,
							"last_rewarded.voice": 0.0,
							"last_rewarded.got_reaction": 0.0,
							"last_rewarded.give_reaction": 0.0,
						}

						# Prune any paths that appear in other operators to avoid conflicts
						conflict_paths = set(inc_updates.keys()) | set(set_updates.keys()) | set(max_updates.keys())
						set_on_insert = {k: v for k, v in set_on_insert_full.items() if k not in conflict_paths}

						# Assemble update
						update_doc: Dict[str, Any] = {}
						if inc_updates:
							update_doc["$inc"] = inc_updates
						if set_updates:
							update_doc["$set"] = set_updates
						if max_updates:
							update_doc["$max"] = max_updates
						if set_on_insert:
							update_doc["$setOnInsert"] = set_on_insert

						if update_doc:
							operations_batch.append(
								UpdateOne({"guild_id": guild_id, "user_id": user_id}, update_doc, upsert=True)
							)
							user_ops_count += 1

						# Bulk in chunks
						if len(operations_batch) >= self.bulk_batch_size:
							logger.debug(f"{s}Executing bulk batch of {len(operations_batch)} operations")
							await self._commit_bulk(operations_batch)
							total_ops += len(operations_batch)
							operations_batch.clear()

					guild_elapsed = time.time() - guild_start
					guild_stats[guild_id] = {
						'users': len(users),
						'operations': user_ops_count,
						'elapsed_ms': round(guild_elapsed * 1000, 2)
					}

					logger.debug(f"{s}Guild {guild_id} processed: {len(users)} users, "
								 f"{user_ops_count} ops, {guild_elapsed:.2f}s")

				# Commit remaining
				if operations_batch:
					logger.debug(f"{s}Executing final bulk batch of {len(operations_batch)} operations")
					await self._commit_bulk(operations_batch)
					total_ops += len(operations_batch)
					operations_batch.clear()

				# Clear caches after successful flush
				self._clear_caches()

				elapsed = time.time() - start_time

				# Detailed completion log
				logger.info(
					f"{s}Flush completed successfully:"
					f"\n{s}  - Total operations: {total_ops}"
					f"\n{s}  - Guilds processed: {len(all_guilds)}"
					f"\n{s}  - Total elapsed: {elapsed:.3f}s"
					f"\n{s}  - Ops/second: {total_ops / elapsed:.1f}" if elapsed > 0 else ""
				)

				# Per-guild breakdown for debug
				if logger.isEnabledFor(10):  # DEBUG level
					for guild_id, stats in guild_stats.items():
						logger.debug(f"{s}Guild {guild_id}: {stats['users']} users, "
									 f"{stats['operations']} ops, {stats['elapsed_ms']}ms")

			except Exception as e:
				elapsed = time.time() - start_time
				logger.error(f"{s}flush_to_db failed after {elapsed:.3f}s: {e}")
				logger.error(f"{s}Operations attempted: {total_ops}, Cache stats: {cache_stats}")
				# Don't re-raise to allow next flush cycle to retry

	async def _commit_bulk(self, ops: List[UpdateOne]):
		"""Execute bulk write operations with detailed logging."""
		if not ops:
			logger.warning(f"{s}_commit_bulk called with empty operations list")
			return

		operation_start = time.time()

		try:
			with PerformanceLogger(logger, f"bulk-write-{len(ops)}-ops"):
				result = await self.collection.bulk_write(ops, ordered=False)

				operation_elapsed = time.time() - operation_start

				logger.info(
					f"{s}Bulk write completed: "
					f"ops={len(ops)}, matched={result.matched_count}, "
					f"modified={result.modified_count}, upserted={len(result.upserted_ids)}, "
					f"elapsed={operation_elapsed:.3f}s"
				)

				# Log operation details at debug level
				if logger.isEnabledFor(10):  # DEBUG level
					inc_ops = sum(1 for op in ops if "$inc" in getattr(op, "_doc", {}))
					set_ops = sum(1 for op in ops if "$set" in getattr(op, "_doc", {}))
					max_ops = sum(1 for op in ops if "$max" in getattr(op, "_doc", {}))

					logger.debug(
						f"{s}Operation breakdown: $inc={inc_ops}, $set={set_ops}, $max={max_ops}, "
						f"throughput={len(ops) / operation_elapsed:.1f} ops/s"
					)

		except Exception as e:
			operation_elapsed = time.time() - operation_start
			logger.error(
				f"{s}Bulk write failed: ops={len(ops)}, elapsed={operation_elapsed:.3f}s, error={e}"
			)
			# Log some operation details for debugging
			if len(ops) > 0:
				sample_op = ops[0]
				sample_filter = getattr(sample_op, "_filter", {})
				sample_doc = getattr(sample_op, "_doc", {})
				logger.error(f"{s}Sample operation - filter: {sample_filter}, doc keys: {list(sample_doc.keys())}")

	def _clear_caches(self):
		"""Clear all caches with logging."""
		cache_counts = {
			'message_cache': sum(len(users) for users in self.message_cache.values()),
			'voice_cache': sum(len(users) for users in self.voice_cache.values()),
			'voice_stats_cache': len(self.voice_stats_cache),
			'emoji_favorites_cache': sum(len(users) for users in self.emoji_favorites_cache.values())
		}

		logger.debug(f"{s}Clearing caches - counts before clear: {cache_counts}")

		self.message_cache.clear()
		self.voice_cache.clear()
		self.message_length_cache.clear()
		self.last_message_time.clear()
		self.daily_streak_cache.clear()
		self.reacted_messages_cache.clear()
		self.got_reactions_cache.clear()
		self.voice_stats_cache.clear()
		self.emoji_favorites_cache.clear()

		logger.debug(f"{s}All caches cleared successfully")

	# =========================
	# Reporting
	# =========================
	async def get_stats_per_guild(self) -> List[dict]:
		"""
        Returns current (not-yet-flushed) cached aggregates per guild.
        """
		logger.debug(f"{s}get_stats_per_guild called")

		stats: List[dict] = []
		guild_ids = set(
			list(self.message_cache.keys()) +
			list(self.voice_cache.keys()) +
			list(self.message_length_cache.keys()) +
			list(self.daily_streak_cache.keys())
		)

		for guild_id in guild_ids:
			guild_stats = {
				"guild_id": guild_id,
				"messages": sum(self.message_cache[guild_id].values()) if guild_id in self.message_cache else 0,
				"voice_seconds": sum(self.voice_cache[guild_id].values()) if guild_id in self.voice_cache else 0.0,
				"longest_message": max(self.message_length_cache[guild_id].values(), default=0)
				if guild_id in self.message_length_cache else 0,
				"daily_streak": max(self.daily_streak_cache[guild_id].values(), default=0)
				if guild_id in self.daily_streak_cache else 0,
			}
			stats.append(guild_stats)

		logger.debug(f"{s}get_stats_per_guild returning stats for {len(stats)} guilds")
		return stats

	async def get_user_stats(
			self,
			guild_id: str,
			user_id: str,
			*,
			flush: bool = True,
			include_cache: bool = True,
	) -> dict:
		"""
        Get a user's stats with an option to:
          - flush: ensure DB is up-to-date before reading
          - include_cache: overlay in-memory (not-yet-flushed) deltas

        Returns a complete document-like dict (with defaults) for the user.
        """
		logger.debug(
			f"{s}get_user_stats called: guild={guild_id}, user={user_id}, flush={flush}, include_cache={include_cache}")

		try:
			if flush:
				logger.debug(f"{s}Performing flush before user stats retrieval")
				# Ensure the DB reflects the latest aggregates before reading
				await self.flush_to_db()
		except Exception as e:
			logger.error(f"{s}get_user_stats flush error for user {user_id}: {e}")

		# Fetch from DB
		try:
			with PerformanceLogger(logger, f"fetch-user-stats-{user_id}"):
				doc = await self.collection.find_one({"guild_id": guild_id, "user_id": user_id})

			if not doc:
				logger.debug(f"{s}No existing document found for user {user_id}, creating default structure")
				doc = self._merge_default_structure(guild_id, user_id, {})
			else:
				logger.debug(f"{s}Found existing document for user {user_id}")

		except Exception as e:
			logger.error(f"{s}Database error fetching user stats for {user_id}: {e}")
			# Return default structure on error
			doc = self._merge_default_structure(guild_id, user_id, {})

		if not include_cache:
			logger.debug(f"{s}Returning user stats without cache overlay")
			return doc

		# Overlay cached values to reflect real-time deltas without creating new cache keys
		try:
			logger.debug(f"{s}Applying cache overlay for user {user_id}")

			# Safe reads from defaultdict caches without mutating them
			msg_cache_g = self.message_cache.get(guild_id, {})
			voice_cache_g = self.voice_cache.get(guild_id, {})
			msg_len_cache_g = self.message_length_cache.get(guild_id, {})
			last_msg_time_g = self.last_message_time.get(guild_id, {})
			streak_cache_g = self.daily_streak_cache.get(guild_id, {})
			reacted_cache_g = self.reacted_messages_cache.get(guild_id, {})
			got_react_cache_g = self.got_reactions_cache.get(guild_id, {})
			emoji_fav_g = self.emoji_favorites_cache.get(guild_id, {})

			# Message stats overlay
			ms = doc.setdefault("message_stats", {})
			cached_messages = int(msg_cache_g.get(user_id, 0))
			if cached_messages > 0:
				logger.debug(f"{s}User {user_id} has {cached_messages} cached messages")

			ms["messages"] = int(ms.get("messages", 0)) + cached_messages
			ms["reacted_messages"] = int(ms.get("reacted_messages", 0)) + int(reacted_cache_g.get(user_id, 0))
			ms["got_reactions"] = int(ms.get("got_reactions", 0)) + int(got_react_cache_g.get(user_id, 0))

			cached_longest = int(msg_len_cache_g.get(user_id, 0) or 0)
			ms["longest_message"] = max(int(ms.get("longest_message", 0) or 0), cached_longest)

			cached_last_ts = float(last_msg_time_g.get(user_id, 0.0) or 0.0)
			ms["last_message_time"] = max(float(ms.get("last_message_time", 0.0) or 0.0), cached_last_ts)

			# If we computed a new streak in cache, prefer it for quick reads
			cached_streak = int(streak_cache_g.get(user_id, 0) or 0)
			if cached_streak > 0:
				ms["daily_streak"] = cached_streak

			# Favorites overlay (incremental)
			fav = doc.setdefault("favorites", {})
			user_favs = (emoji_fav_g.get(user_id, {}) or {})
			if user_favs:
				logger.debug(f"{s}User {user_id} has {len(user_favs)} cached favorite emojis")
			for emoji, cnt in user_favs.items():
				fav[emoji] = int(fav.get(emoji, 0)) + int(cnt or 0)

			# Voice overlay
			vs_doc = doc.setdefault("voice_stats", {})
			cached_voice = float(voice_cache_g.get(user_id, 0.0) or 0.0)
			if cached_voice > 0:
				logger.debug(f"{s}User {user_id} has {cached_voice:.1f} cached voice seconds")

			# Aggregate voice_seconds based on coarse and detailed caches
			vs_doc["voice_seconds"] = float(vs_doc.get("voice_seconds", 0.0) or 0.0) + cached_voice

			key = f"{guild_id}:{user_id}"
			if key in self.voice_stats_cache:
				vs = self.voice_stats_cache[key]
				# Add deltas for detailed metrics
				vs_doc["active_seconds"] = float(vs_doc.get("active_seconds", 0.0) or 0.0) + float(
					vs.get("active_seconds", 0.0) or 0.0)
				vs_doc["muted_time"] = float(vs_doc.get("muted_time", 0.0) or 0.0) + float(
					vs.get("muted_time", 0.0) or 0.0)
				vs_doc["deafened_time"] = float(vs_doc.get("deafened_time", 0.0) or 0.0) + float(
					vs.get("deafened_time", 0.0) or 0.0)
				vs_doc["self_muted_time"] = float(vs_doc.get("self_muted_time", 0.0) or 0.0) + float(
					vs.get("self_muted_time", 0.0) or 0.0)
				vs_doc["self_deafened_time"] = float(vs_doc.get("self_deafened_time", 0.0) or 0.0) + float(
					vs.get("self_deafened_time", 0.0) or 0.0)
				vs_doc["voice_sessions"] = int(vs_doc.get("voice_sessions", 0) or 0) + int(
					vs.get("sessions", 0) or 0)

				# For percentages we mirror the running average from the session cache if available
				if int(vs.get("sessions", 0) or 0) > 0:
					vs_doc["total_active_percentage"] = float(vs.get("total_active_percentage", 0.0) or 0.0)
					vs_doc["total_unmuted_percentage"] = float(vs.get("total_unmuted_percentage", 0.0) or 0.0)

			logger.debug(f"{s}Cache overlay completed for user {user_id}")

		except Exception as e:
			logger.error(f"{s}get_user_stats overlay error for user {user_id}: {e}")
			# Continue without overlay rather than failing

		return doc

	# =========================
	# Cleanup
	# =========================
	async def cleanup(self):
		"""Perform a final flush of all caches and cleanup on shutdown"""
		logger.info(f"{s}🔄 Starting TrackManager cleanup...")

		try:
			logger.info(f"{s}Performing final cache flush before shutdown...")
			with PerformanceLogger(logger, "final-cleanup-flush"):
				await self.flush_to_db()
			logger.info(f"{s}Final cache flush completed successfully")

		except Exception as e:
			logger.error(f"{s}Error during final cache flush: {e}")
		finally:
			try:
				if self._flush_task:
					logger.info(f"{s}Stopping auto-flush task...")
					self.stop_auto_flush()

				logger.info(f"{s}Closing MongoDB client...")
				self.mongo_client.close()
				logger.info(f"{s}MongoDB client closed successfully")

			except Exception as e:
				logger.error(f"{s}Error during MongoDB client cleanup: {e}")

		logger.info(f"{s}TrackManager cleanup completed")


track_manager = TrackManager()