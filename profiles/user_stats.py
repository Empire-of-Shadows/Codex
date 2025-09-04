import asyncio
import time
import discord
from discord import app_commands
from discord.ext import commands
from datetime import timedelta
from Database.DatabaseManager import db_manager
from profiles.profile import ProfilePreferences, create_profile_card, ProfileCardView
from io import BytesIO
import pendulum

from utils.logger import get_logger

logger = get_logger("UserStats")

# Timeouts and log verbosity
FETCH_TIMEOUT_SECONDS = 6.0
SEND_TIMEOUT_SECONDS = 6.0
LOG_PREFIX = "[UserStats]"


def _now() -> float:
	return time.monotonic()


def _fmt(td: float) -> str:
	return f"{td * 1000:.1f}ms"


def _calculate_xp_progress(level: int, current_xp: int) -> tuple[int, int]:
	"""
    Calculate XP progress using the actual leveling system formula.
    Returns (xp_in_current_level, xp_needed_for_next_level)
    """
	logger.debug(f"{LOG_PREFIX} Calculating XP progress for level={level}, current_xp={current_xp}")

	# Using the same formula as leveling.py: 50 * level * (level + 1)
	current_level_total_xp = 50 * level * (level + 1)
	next_level_total_xp = 50 * (level + 1) * (level + 2)

	xp_needed_for_next = next_level_total_xp - current_level_total_xp
	xp_progress_in_level = max(0, current_xp - current_level_total_xp)

	logger.debug(f"{LOG_PREFIX} XP calculation result: progress={xp_progress_in_level}, needed={xp_needed_for_next}")
	return xp_progress_in_level, xp_needed_for_next


class UserStatsView(discord.ui.View):
	"""
    Interactive view for navigating user stats sections and refreshing data.
    Sections: overview, messages, voice, emojis
    """

	def __init__(self, member: discord.Member, stats: dict, *, timeout: int = 180):
		super().__init__(timeout=timeout)
		self.member = member
		self.stats = stats or {}
		self.current_section = "overview"

		logger.info(f"{LOG_PREFIX} UserStatsView initializing for member={member.id} ({member.display_name})")
		logger.debug(f"{LOG_PREFIX} Stats keys available: {list(self.stats.keys()) if self.stats else 'None'}")

		try:
			# Create and add components with error handling
			logger.debug(f"{LOG_PREFIX} Creating SectionSelect...")
			section_select = SectionSelect(self)
			self.add_item(section_select)
			logger.debug(f"{LOG_PREFIX} SectionSelect added successfully")

			logger.debug(f"{LOG_PREFIX} Creating RefreshButton...")
			refresh_button = RefreshButton(self)
			self.add_item(refresh_button)
			logger.debug(f"{LOG_PREFIX} RefreshButton added successfully")

			logger.info(f"{LOG_PREFIX} UserStatsView initialized successfully for member={member.id}")
		except Exception as e:
			logger.error(f"{LOG_PREFIX} Error during UserStatsView init for member={member.id}: {e}", exc_info=True)
			raise

	async def build_embed(self) -> discord.Embed:
		start_time = _now()
		logger.debug(f"{LOG_PREFIX} Building embed for section='{self.current_section}', user={self.member.id}")

		try:
			ms = (self.stats.get("message_stats") or {})
			vs = (self.stats.get("voice_stats") or {})
			fav = (self.stats.get("favorites") or {})

			logger.debug(f"{LOG_PREFIX} Stats data sizes: messages={len(ms)}, voice={len(vs)}, favorites={len(fav)}")

			embed = discord.Embed(
				title=f"📊 Stats for {self.member.display_name}",
				color=discord.Color.blue()
			)

			if self.current_section == "overview":
				logger.debug(f"{LOG_PREFIX} Building overview section")
				try:
					# Basic stats
					level = self.stats.get('level', 0)
					xp = self.stats.get('xp', 0)
					embers = self.stats.get('embers', 0)

					embed.add_field(
						name="",
						value=(
							f"**Level:** {level}\n"
							f"**XP:** {xp:,}\n"
							f"**Embers:** {embers:,}\n"
						)
					)
					logger.debug(f"{LOG_PREFIX} Basic stats added: level={level}, xp={xp}, embers={embers}")

					# Message stats
					messages = int(ms.get('messages', 0))
					longest_msg = int(ms.get('longest_message', 0))
					daily_streak = int(ms.get('daily_streak', 0))
					reacted_msgs = int(ms.get('reacted_messages', 0))
					got_reactions = int(ms.get('got_reactions', 0))

					embed.add_field(
						name="💬 Message Stats",
						value=(
							f"**Messages Sent:** {messages:,}\n"
							f"**Longest Message:** {longest_msg:,} characters\n"
							f"**Daily Streak:** {daily_streak:,} days\n"
							f"**Reacted Messages:** {reacted_msgs:,}\n"
							f"**Reactions Received:** {got_reactions:,}\n"
						),
						inline=False
					)
					logger.debug(f"{LOG_PREFIX} Message stats added: messages={messages}, streak={daily_streak}")

					# Voice stats
					voice_time = int(vs.get("voice_seconds", 0))
					active_time = int(vs.get("active_seconds", 0))
					muted_time = int(vs.get("muted_time", 0))
					deaf_time = int(vs.get("deafened_time", 0))
					sessions = int(vs.get("voice_sessions", 0))
					apct = float(vs.get("total_active_percentage", 0) or 0.0)
					upct = float(vs.get("total_unmuted_percentage", 0) or 0.0)

					embed.add_field(
						name="🔊 Voice Stats",
						value=(
							f"**Voice Time:** {str(timedelta(seconds=voice_time))}\n"
							f"**Active Time:** {str(timedelta(seconds=active_time))}\n"
							f"**Muted Time:** {str(timedelta(seconds=muted_time))}\n"
							f"**Deafened Time:** {str(timedelta(seconds=deaf_time))}\n"
							f"**Sessions:** {sessions:,}\n"
							f"**Active Percentage:** {apct:.2f}%\n"
							f"**Unmuted Percentage:** {upct:.2f}%\n"
						),
						inline=False
					)
					logger.debug(f"{LOG_PREFIX} Voice stats added: sessions={sessions}, voice_time={voice_time}s")

					# Top 5 emojis preview - with safety measures
					try:
						emoji_process_start = _now()
						# Limit processing to avoid hanging on huge datasets
						if len(fav) > 10000:
							logger.warning(f"{LOG_PREFIX} Large emoji dataset ({len(fav)} items), limiting processing")
							# Take a sample first to avoid processing massive datasets
							fav_sample = dict(list(fav.items())[:1000])
							sorted_favorites = sorted(fav_sample.items(), key=lambda item: item[1], reverse=True)[:5]
						else:
							sorted_favorites = sorted(fav.items(), key=lambda item: item[1], reverse=True)[:5]

						emojis_preview_lines = []
						for emoji, count in sorted_favorites:
							# Sanitize emoji string to prevent issues
							emoji_str = str(emoji)[:50]  # Limit length
							if len(emoji_str.encode('utf-8')) > 100:  # Limit byte size
								emoji_str = emoji_str[:20] + "..."
							emojis_preview_lines.append(f"{emoji_str}: {count}")

						emojis_preview = "\n".join(emojis_preview_lines) or "None"
						embed.add_field(name="⭐ Favorite Emojis (Top 5)", value=emojis_preview, inline=False)

						emoji_process_time = _now() - emoji_process_start
						logger.debug(
							f"{LOG_PREFIX} Emoji preview processed in {_fmt(emoji_process_time)}: {len(sorted_favorites)} items")
					except Exception as emoji_error:
						logger.error(
							f"{LOG_PREFIX} Error processing emoji preview for user={self.member.id}: {emoji_error}",
							exc_info=True)
						embed.add_field(name="⭐ Favorite Emojis (Top 5)", value="Error loading emojis", inline=False)

				except Exception as overview_error:
					logger.error(
						f"{LOG_PREFIX} Error building overview section for user={self.member.id}: {overview_error}",
						exc_info=True)
					raise

			elif self.current_section == "messages":
				logger.debug(f"{LOG_PREFIX} Building messages detail section")
				embed.add_field(
					name="💬 Message Stats (Detail)",
					value=(
						f"**Messages Sent:** {int(ms.get('messages', 0)):,}\n"
						f"**Longest Message:** {int(ms.get('longest_message', 0)):,} characters\n"
						f"**Daily Streak:** {int(ms.get('daily_streak', 0)):,} days\n"
						f"**Reacted Messages:** {int(ms.get('reacted_messages', 0)):,}\n"
						f"**Reactions Received:** {int(ms.get('got_reactions', 0)):,}\n"
					),
					inline=False
				)

			elif self.current_section == "voice":
				logger.debug(f"{LOG_PREFIX} Building voice detail section")
				voice_time = int(vs.get("voice_seconds", 0))
				active_time = int(vs.get("active_seconds", 0))
				muted_time = int(vs.get("muted_time", 0))
				deaf_time = int(vs.get("deafened_time", 0))
				self_muted_time = int(vs.get("self_muted_time", 0))
				self_deaf_time = int(vs.get("self_deafened_time", 0))
				sessions = int(vs.get("voice_sessions", 0))
				apct = float(vs.get("total_active_percentage", 0) or 0.0)
				upct = float(vs.get("total_unmuted_percentage", 0) or 0.0)
				embed.add_field(
					name="🔊 Voice Stats (Detail)",
					value=(
						f"**Voice:** {str(timedelta(seconds=voice_time))}\n"
						f"**Active:** {str(timedelta(seconds=active_time))}\n"
						f"**Muted:** {str(timedelta(seconds=muted_time))}\n"
						f"**Deafened:** {str(timedelta(seconds=deaf_time))}\n"
						f"**Self-Muted:** {str(timedelta(seconds=self_muted_time))}\n"
						f"**Self-Deafened:** {str(timedelta(seconds=self_deaf_time))}\n"
						f"**Sessions:** {sessions:,}\n"
						f"**Active%:** {apct:.2f}%  **Unmuted%:** {upct:.2f}%"
					),
					inline=False
				)

			elif self.current_section == "emojis":
				logger.debug(f"{LOG_PREFIX} Building emojis detail section")
				try:
					# Safe emoji processing for full list
					emoji_process_start = _now()
					if len(fav) > 10000:
						logger.warning(f"{LOG_PREFIX} Large emoji dataset for emojis section ({len(fav)} items)")
						fav_sample = dict(list(fav.items())[:2000])
						sorted_fav = sorted(fav_sample.items(), key=lambda item: item[1], reverse=True)[:10]
					else:
						sorted_fav = sorted(fav.items(), key=lambda item: item[1], reverse=True)[:10]

					emoji_lines = []
					for emoji, count in sorted_fav:
						emoji_str = str(emoji)[:50]
						if len(emoji_str.encode('utf-8')) > 100:
							emoji_str = emoji_str[:20] + "..."
						emoji_lines.append(f"{emoji_str}: {count}")

					top = "\n".join(emoji_lines) or "None"
					embed.add_field(name="⭐ Top Emojis (Top 10)", value=top, inline=False)

					emoji_process_time = _now() - emoji_process_start
					logger.debug(
						f"{LOG_PREFIX} Full emoji list processed in {_fmt(emoji_process_time)}: {len(sorted_fav)} items")
				except Exception as emoji_error:
					logger.error(
						f"{LOG_PREFIX} Error processing full emoji list for user={self.member.id}: {emoji_error}",
						exc_info=True)
					embed.add_field(name="⭐ Top Emojis (Top 10)", value="Error loading emojis", inline=False)

			build_time = _now() - start_time
			logger.info(
				f"{LOG_PREFIX} Embed built successfully in {_fmt(build_time)} for section='{self.current_section}', user={self.member.id}")
			return embed

		except Exception as e:
			build_time = _now() - start_time
			logger.error(
				f"{LOG_PREFIX} Failed to build embed after {_fmt(build_time)} for section='{self.current_section}', user={self.member.id}: {e}",
				exc_info=True)
			# Return a basic error embed instead of failing completely
			error_embed = discord.Embed(
				title=f"📊 Stats for {self.member.display_name}",
				description="⚠️ Error loading stats. Please try again.",
				color=discord.Color.red()
			)
			return error_embed


class SectionSelect(discord.ui.Select):
	def __init__(self, view: UserStatsView):
		logger.debug(f"{LOG_PREFIX} SectionSelect initializing")

		options = [
			discord.SelectOption(label="Overview", value="overview", emoji="📊"),
			discord.SelectOption(label="Messages", value="messages", emoji="💬"),
			discord.SelectOption(label="Voice", value="voice", emoji="🔊"),
			discord.SelectOption(label="Emojis", value="emojis", emoji="⭐"),
		]

		super().__init__(placeholder="Switch section...", options=options, row=0)
		self.view_ref = view
		logger.debug(f"{LOG_PREFIX} SectionSelect initialized with {len(options)} options")

	async def callback(self, interaction: discord.Interaction):
		start_time = _now()
		logger.info(f"{LOG_PREFIX} SectionSelect callback triggered by user={interaction.user.id}")

		try:
			# Use the new interaction_metadata instead of deprecated interaction
			origin_interaction_metadata = getattr(interaction.message, "interaction_metadata", None)
			origin_user = getattr(origin_interaction_metadata, "user", None) if origin_interaction_metadata else None
			origin_id = getattr(origin_user, "id", interaction.user.id)

			logger.debug(
				f"{LOG_PREFIX} SectionSelect access check: clicker={interaction.user.id}, origin={origin_id}, "
				f"target={self.view_ref.member.id}, switching to='{self.values[0]}'"
			)

			if interaction.user.id not in {self.view_ref.member.id, origin_id}:
				logger.warning(f"{LOG_PREFIX} Unauthorized section switch attempt by user={interaction.user.id}")
				return await interaction.response.send_message("This view isn't for you.", ephemeral=True) # type: ignore

			old_section = self.view_ref.current_section
			self.view_ref.current_section = self.values[0]

			logger.info(
				f"{LOG_PREFIX} Section changed from '{old_section}' to '{self.view_ref.current_section}' by user={interaction.user.id}")

			t0 = _now()
			embed = await self.view_ref.build_embed()
			embed_time = _now() - t0

			await interaction.response.edit_message(embed=embed, view=self.view_ref) # type: ignore

			total_time = _now() - start_time
			logger.info(f"{LOG_PREFIX} Section switch completed in {_fmt(total_time)} (embed: {_fmt(embed_time)})")

		except Exception as e:
			total_time = _now() - start_time
			logger.error(f"{LOG_PREFIX} SectionSelect callback failed after {_fmt(total_time)}: {e}", exc_info=True)
			try:
				await interaction.response.send_message("An error occurred while switching sections.", ephemeral=True) # type: ignore
			except Exception as resp_error:
				logger.error(f"{LOG_PREFIX} Failed to send error response: {resp_error}")


class RefreshButton(discord.ui.Button):
	def __init__(self, view: UserStatsView):
		logger.debug(f"{LOG_PREFIX} RefreshButton initializing")
		super().__init__(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄", row=1) # type: ignore
		self.view_ref = view
		logger.debug(f"{LOG_PREFIX} RefreshButton initialized")

	async def callback(self, interaction: discord.Interaction):
		start_time = _now()
		logger.info(
			f"{LOG_PREFIX} Refresh requested by user={interaction.user.id}, guild={getattr(interaction.guild, 'id', None)}, target_member={self.view_ref.member.id}")

		try:
			if not interaction.guild:
				logger.warning(f"{LOG_PREFIX} Refresh rejected - no guild context")
				return await interaction.response.send_message("This action is only available in a server.", # type: ignore
															   ephemeral=True)

			logger.debug(f"{LOG_PREFIX} Starting stats refresh for member={self.view_ref.member.id}")
			t0 = _now()

			# Use DatabaseManager to fetch user stats
			user_stats_collection = db_manager.get_raw_collection_from_connection('Ecom-Server', 'Users', 'secondary')
			refreshed = await asyncio.wait_for(
				user_stats_collection.find_one(
					{
						"guild_id": str(interaction.guild.id),
						"user_id": str(self.view_ref.member.id)
					}
				),
				timeout=FETCH_TIMEOUT_SECONDS
			)

			fetch_time = _now() - t0

			stats_size = len(str(refreshed)) if refreshed else 0
			logger.info(f"{LOG_PREFIX} Stats refresh completed in {_fmt(fetch_time)} - data size: {stats_size} bytes")

			self.view_ref.stats = refreshed or {}

			logger.debug(f"{LOG_PREFIX} Building refreshed embed")
			embed_start = _now()
			embed = await self.view_ref.build_embed()
			embed_time = _now() - embed_start

			logger.debug(f"{LOG_PREFIX} Sending refreshed message")
			await interaction.response.edit_message(embed=embed, view=self.view_ref) # type: ignore

			total_time = _now() - start_time
			logger.info(
				f"{LOG_PREFIX} Refresh completed successfully in {_fmt(total_time)} (fetch: {_fmt(fetch_time)}, embed: {_fmt(embed_time)})")

		except asyncio.TimeoutError:
			total_time = _now() - start_time
			logger.warning(
				f"{LOG_PREFIX} Refresh timeout after {_fmt(total_time)} for member={self.view_ref.member.id}")

			try:
				if interaction.response.is_done(): # type: ignore
					await interaction.followup.send("Fetching stats is taking too long. Please try again shortly.",
													ephemeral=True)
				else:
					await interaction.response.send_message( # type: ignore
						"Fetching stats is taking too long. Please try again shortly.", ephemeral=True)
			except Exception as resp_error:
				logger.error(f"{LOG_PREFIX} Failed to send timeout response: {resp_error}")

		except Exception as e:
			total_time = _now() - start_time
			logger.error(
				f"{LOG_PREFIX} Refresh failed after {_fmt(total_time)} for member={self.view_ref.member.id}: {e}",
				exc_info=True)

			try:
				if interaction.response.is_done(): # type: ignore
					await interaction.followup.send("Couldn't refresh stats right now.", ephemeral=True)
				else:
					await interaction.response.send_message("Couldn't refresh stats right now.", ephemeral=True) # type: ignore
			except Exception as resp_error:
				logger.error(f"{LOG_PREFIX} Failed to send error response: {resp_error}")


class MemberCommands(commands.Cog):
	"""
	Independent cog that handles all member-related commands (card, settings, stats).
	"""

	def __init__(self, bot: commands.Bot):
		self.bot = bot
		self.preferences = None
		logger.info(f"{LOG_PREFIX} MemberCommands cog initializing")

	async def cog_load(self):
		"""Initialize the cog."""
		logger.info(f"{LOG_PREFIX} Loading MemberCommands cog")
		try:
			await db_manager.initialize()
			self.preferences = ProfilePreferences()
			logger.info(f"{LOG_PREFIX} MemberCommands cog loaded successfully")
		except Exception as e:
			logger.error(f"{LOG_PREFIX} Failed to load MemberCommands cog: {e}", exc_info=True)
			raise

	async def _fetch_user_data(self, user: discord.User, guild: discord.Guild) -> dict:
		"""Efficiently fetch all user data in parallel."""
		start_time = _now()
		user_id_str = str(user.id)
		guild_id_str = str(guild.id)

		logger.debug(f"{LOG_PREFIX} Fetching user data for user={user.id} in guild={guild.id}")

		# Use DatabaseManager for all data fetching
		try:
			# Get collection managers from the new DatabaseManager
			users_manager = db_manager.get_collection_manager('serverdata_users')

			# For collections not yet configured in DatabaseManager, use raw collection access
			user_stats_collection = db_manager.get_raw_collection_from_connection('Ecom-Server', 'Users', 'secondary')
			inventory_collection = db_manager.get_raw_collection_from_connection('Ecom-Server', 'Inventory',
																				 'secondary')

			# Parallel database queries
			member_task = users_manager.find_one(
				{"guild_id": guild.id, "id": user.id},
				{"display_name": 1, "joined_at": 1, "avatar_url": 1}
			)
			stats_task = user_stats_collection.find_one(
				{"guild_id": guild_id_str, "user_id": user_id_str},
				{
					"level": 1, "xp": 1, "embers": 1,
					"message_stats.messages": 1, "message_stats.daily_streak": 1,
					"voice_stats.voice_seconds": 1, "voice_stats.voice_sessions": 1,
					"favorites": 1
				}
			)
			inventory_task = inventory_collection.count_documents({"user_id": user_id_str})

			# Await all tasks concurrently
			member_data, stats_doc, inv_count = await asyncio.gather(
				member_task, stats_task, inventory_task, return_exceptions=True
			)
			fetch_time = _now() - start_time
			logger.debug(f"{LOG_PREFIX} Database queries completed in {_fmt(fetch_time)}")
		except Exception as e:
			fetch_time = _now() - start_time
			logger.error(f"{LOG_PREFIX} Database queries failed after {_fmt(fetch_time)}: {e}", exc_info=True)
			raise

		# Process member data
		if isinstance(member_data, Exception) or not member_data:
			if isinstance(member_data, Exception):
				logger.warning(f"{LOG_PREFIX} Member data query failed: {member_data}")
			nickname = user.name
			join_date = user.joined_at.strftime("MMMM DD, YYYY") if user.joined_at else "Unknown"
			avatar_url = user.display_avatar.url
		else:
			nickname = member_data.get("display_name", user.name)
			join_date = member_data.get("joined_at", "Unknown")
			avatar_url = member_data.get("avatar_url", user.display_avatar.url)
			if join_date != "Unknown":
				try:
					join_date = pendulum.parse(join_date).format("MMMM DD, YYYY")
				except Exception as date_error:
					logger.warning(f"{LOG_PREFIX} Failed to parse join date '{join_date}': {date_error}")
					join_date = "Unknown"

		# Process stats data
		level = xp = embers = messages = streak = 0
		voice_seconds = 0.0
		voice_sessions = 0
		fav_tuple = None

		if isinstance(stats_doc, Exception):
			logger.warning(f"{LOG_PREFIX} Stats query failed for user={user.id}: {stats_doc}")
		elif stats_doc:
			level = int(stats_doc.get("level", 0) or 0)
			xp = int(stats_doc.get("xp", 0) or 0)
			embers = int(stats_doc.get("embers", 0) or 0)

			ms = stats_doc.get("message_stats", {}) or {}
			messages = int(ms.get("messages", 0) or 0)
			streak = int(ms.get("daily_streak", 0) or 0)

			vs = stats_doc.get("voice_stats", {}) or {}
			voice_seconds = float(vs.get("voice_seconds", 0.0) or 0.0)
			voice_sessions = int(vs.get("voice_sessions", 0) or 0)

			fav = stats_doc.get("favorites", {}) or {}
			if isinstance(fav, dict) and fav:
				try:
					top_emoji, top_count = max(fav.items(), key=lambda kv: int(kv[1] or 0))
					fav_tuple = (str(top_emoji), int(top_count))
				except Exception as fav_error:
					logger.debug(f"{LOG_PREFIX} Failed to get top emoji: {fav_error}")
		else:
			logger.debug(f"{LOG_PREFIX} No stats document found for user={user.id}")

		# Process inventory count
		if isinstance(inv_count, Exception):
			logger.warning(f"{LOG_PREFIX} Inventory count failed for user={user.id}: {inv_count}")
			inv_count = None

		# Calculate XP progress
		xp_progress, xp_needed = _calculate_xp_progress(level, xp)

		total_time = _now() - start_time
		logger.info(f"{LOG_PREFIX} User data fetched successfully in {_fmt(total_time)} for user={user.id}")

		return {
			"nickname": nickname,
			"avatar_url": str(avatar_url),
			"join_date": join_date,
			"level": level,
			"xp_progress": xp_progress,
			"xp_needed": xp_needed,
			"embers": embers,
			"messages": messages,
			"streak": streak,
			"voice_seconds": voice_seconds,
			"voice_sessions": voice_sessions,
			"favorite": fav_tuple,
			"inventory_count": inv_count
		}

	# Member command group
	member_group = app_commands.Group(name="member", description="Member profile and stats commands")

	@member_group.command(name="card", description="Generate a member's profile card")
	@app_commands.describe(
		member="Member to view (defaults to you)",
		layout="Card layout (overrides saved preference)",
		show_inventory="Show inventory summary (overrides saved preference)",
		show_badges="Show badges (overrides saved preference)",
		public="Send as a public message"
	)
	@app_commands.choices(
		layout=[app_commands.Choice(name="Detailed", value="detailed"),
				app_commands.Choice(name="Compact", value="compact")]
	)
	async def member_card(
			self,
			interaction: discord.Interaction,
			member: discord.Member = None,
			layout: app_commands.Choice[str] | None = None,
			show_inventory: bool | None = None,
			show_badges: bool | None = None,
			public: bool = False,
	):
		"""Generates and sends a member's profile card with saved preferences."""
		start_time = _now()
		user = member or interaction.user

		logger.info(
			f"{LOG_PREFIX} Profile card requested for user={user.id} ({user.display_name}) by requester={interaction.user.id}, public={public}")

		await interaction.response.defer(ephemeral=not public) # type: ignore

		try:
			user_id_str = str(user.id)
			guild_id_str = str(interaction.guild.id)

			logger.debug(f"{LOG_PREFIX} Starting parallel data fetch for profile card")
			# Parallel fetch: user data and preferences
			user_data_task = self._fetch_user_data(user, interaction.guild)
			prefs_task = self.preferences.get_user_preferences(user_id_str, guild_id_str)

			user_data, saved_prefs = await asyncio.gather(user_data_task, prefs_task)

			logger.debug(f"{LOG_PREFIX} Data fetched, saved preferences: {saved_prefs}")

			# Use command parameters or saved preferences
			final_theme = saved_prefs["theme"]
			final_layout = layout.value if layout else saved_prefs["layout"]
			final_show_inventory = show_inventory if show_inventory is not None else saved_prefs["show_inventory"]
			final_show_badges = show_badges if show_badges is not None else saved_prefs["show_badges"]

			logger.debug(
				f"{LOG_PREFIX} Final settings: theme={final_theme}, layout={final_layout}, inventory={final_show_inventory}, badges={final_show_badges}")

			# Get theme palette
			theme_palette = await self.preferences.get_theme_palette(final_theme, user_id_str, guild_id_str)

			logger.debug(f"{LOG_PREFIX} Generating profile card image")
			card_start = _now()
			# Generate the card
			image = await create_profile_card(
				user_data=user_data,
				theme_palette=theme_palette,
				layout=final_layout,
				show_inventory=final_show_inventory,
				show_badges=final_show_badges,
			)
			card_time = _now() - card_start

			logger.debug(f"{LOG_PREFIX} Preparing file buffer")
			buffer = BytesIO()
			image.save(buffer, format="PNG", optimize=True)
			buffer.seek(0)
			file = discord.File(buffer, filename=f"profile_card_{user.id}.png")

			view = ProfileCardView(
				user_data, theme_palette,
				theme=final_theme,
				layout=final_layout,
				show_inventory=final_show_inventory,
				show_badges=final_show_badges,
				public=public,
				preferences=self.preferences,
			)

			await interaction.followup.send(
				content=(None if public else f"{user.mention}"),
				file=file,
				view=view,
				ephemeral=not public
			)

			total_time = _now() - start_time
			logger.info(
				f"{LOG_PREFIX} Profile card generated and sent successfully in {_fmt(total_time)} (card generation: {_fmt(card_time)})")

		except Exception as e:
			total_time = _now() - start_time
			logger.error(
				f"{LOG_PREFIX} Profile card generation failed after {_fmt(total_time)} for user={user.id}: {e}",
				exc_info=True)
			try:
				await interaction.followup.send(
					"⚠️ An error occurred while generating the profile card. Please try again later.",
					ephemeral=True
				)
			except Exception as resp_error:
				logger.error(f"{LOG_PREFIX} Failed to send error response: {resp_error}")

	@member_group.command(name="settings", description="Manage your profile card preferences")
	async def member_settings(self, interaction: discord.Interaction):
		"""Show current profile settings and allow management."""
		start_time = _now()
		logger.info(f"{LOG_PREFIX} Settings requested by user={interaction.user.id}")

		await interaction.response.defer(ephemeral=True) # type: ignore

		try:
			prefs = await self.preferences.get_user_preferences(
				str(interaction.user.id), str(interaction.guild.id)
			)

			logger.debug(f"{LOG_PREFIX} Current preferences for user={interaction.user.id}: {prefs}")

			embed = discord.Embed(
				title="🎨 Profile Card Settings",
				description="Your current profile card preferences:",
				color=discord.Color.blue()
			)
			embed.add_field(name="Theme", value=prefs["theme"].title(), inline=True)
			embed.add_field(name="Layout", value=prefs["layout"].title(), inline=True)
			embed.add_field(name="Show Inventory", value="✅" if prefs["show_inventory"] else "❌", inline=True)
			embed.add_field(name="Show Badges", value="✅" if prefs["show_badges"] else "❌", inline=True)

			embed.set_footer(
				text="Use /member card to generate your card with these settings, or use the buttons to modify your current card and save new preferences.")

			await interaction.followup.send(embed=embed, ephemeral=True)

			total_time = _now() - start_time
			logger.info(f"{LOG_PREFIX} Settings displayed successfully in {_fmt(total_time)}")

		except Exception as e:
			total_time = _now() - start_time
			logger.error(f"{LOG_PREFIX} Settings display failed after {_fmt(total_time)}: {e}", exc_info=True)
			try:
				await interaction.followup.send(
					"❌ Failed to load your profile settings. Please try again later.",
					ephemeral=True
				)
			except Exception as resp_error:
				logger.error(f"{LOG_PREFIX} Failed to send error response: {resp_error}")

	@member_group.command(name="stats", description="View stats for yourself or another member")
	@app_commands.describe(member="Member to view (defaults to you)", public="Show publicly (default: off)")
	async def member_stats(self, interaction: discord.Interaction, member: discord.Member = None, public: bool = False):
		"""Displays member stats with sectioned view and refresh support."""
		start_time = _now()
		req_id = f"{getattr(interaction, 'id', 'unknown')}"
		target_member = member or interaction.user

		logger.info(
			f"{LOG_PREFIX} Stats command invoked - req_id={req_id}, guild={getattr(interaction.guild, 'id', None)}, "
			f"requester={interaction.user.id}, target={target_member.id} ({target_member.display_name}), public={public}"
		)

		if not interaction.guild:
			logger.warning(f"{LOG_PREFIX} Stats command rejected (DM usage) - req_id={req_id}")
			return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True) # type: ignore

		# Defer response
		await interaction.response.defer(ephemeral=not public) # type: ignore
		logger.debug(f"{LOG_PREFIX} Response deferred - req_id={req_id}")

		guild_id = str(interaction.guild.id)
		user_id = str(target_member.id)

		# Fetch stats with timeout
		try:
			logger.debug(f"{LOG_PREFIX} Fetching stats - req_id={req_id}")
			fetch_start = _now()

			# Use DatabaseManager to fetch user stats
			user_stats_collection = db_manager.get_raw_collection_from_connection('Ecom-Server', 'Users', 'secondary')
			# Debug the query to see the sent and received result
			query = {"guild_id": guild_id, "user_id": user_id}
			logger.debug(f"{LOG_PREFIX} Fetching stats - req_id={req_id}, query: {query}")
			stats = await asyncio.wait_for(
				user_stats_collection.find_one(query),
				timeout=6.0
			)

			fetch_time = _now() - fetch_start
			stats_size = len(str(stats)) if stats else 0
			logger.info(
				f"{LOG_PREFIX} Stats fetched successfully in {_fmt(fetch_time)} - req_id={req_id}, size={stats_size} bytes")

		except asyncio.TimeoutError:
			fetch_time = _now() - fetch_start
			logger.warning(f"{LOG_PREFIX} Stats fetch timeout after {_fmt(fetch_time)} - req_id={req_id}")
			try:
				await interaction.followup.send(
					"Fetching stats is taking longer than expected. Please try again in a moment.",
					ephemeral=True
				)
			except Exception as resp_error:
				logger.error(f"{LOG_PREFIX} Failed to send timeout response: {resp_error}")
			return

		except Exception as e:
			fetch_time = _now() - fetch_start
			logger.error(f"{LOG_PREFIX} Stats fetch failed after {_fmt(fetch_time)} - req_id={req_id}: {e}",
						 exc_info=True)
			try:
				await interaction.followup.send(
					"⚠️ Unable to fetch member stats at the moment. Please try again later.",
					ephemeral=True
				)
			except Exception as resp_error:
				logger.error(f"{LOG_PREFIX} Failed to send error response: {resp_error}")
			return

		if not stats:
			logger.info(f"{LOG_PREFIX} No stats found - req_id={req_id}")
			try:
				await interaction.followup.send(f"❌ No stats found for {target_member.mention}.", ephemeral=True)
			except Exception as resp_error:
				logger.error(f"{LOG_PREFIX} Failed to send no-stats response: {resp_error}")
			return

		# Build interactive view
		try:
			logger.debug(f"{LOG_PREFIX} Creating UserStatsView - req_id={req_id}")
			view_start = _now()
			view = UserStatsView(target_member, stats)
			view_time = _now() - view_start
			logger.debug(f"{LOG_PREFIX} UserStatsView created successfully in {_fmt(view_time)} - req_id={req_id}")

		except Exception as view_error:
			view_time = _now() - view_start if 'view_start' in locals() else 0
			logger.error(f"{LOG_PREFIX} View creation failed after {_fmt(view_time)} - req_id={req_id}: {view_error}",
						 exc_info=True)
			try:
				await interaction.followup.send("Error creating the stats interface. Please try again.", ephemeral=True)
			except Exception as resp_error:
				logger.error(f"{LOG_PREFIX} Failed to send view error response: {resp_error}")
			return

		logger.debug(f"{LOG_PREFIX} Building initial embed - req_id={req_id}")
		try:
			embed_start = _now()
			embed = await asyncio.wait_for(view.build_embed(), timeout=SEND_TIMEOUT_SECONDS)
			embed_time = _now() - embed_start
			logger.debug(f"{LOG_PREFIX} Initial embed built successfully in {_fmt(embed_time)} - req_id={req_id}")

		except asyncio.TimeoutError:
			embed_time = _now() - embed_start
			logger.warning(f"{LOG_PREFIX} Embed build timeout after {_fmt(embed_time)} - req_id={req_id}")
			try:
				await interaction.followup.send("Preparing the stats view took too long. Please try again.",
												ephemeral=True)
			except Exception as resp_error:
				logger.error(f"{LOG_PREFIX} Failed to send embed timeout response: {resp_error}")
			return

		except Exception as e:
			embed_time = _now() - embed_start if 'embed_start' in locals() else 0
			logger.error(f"{LOG_PREFIX} Embed build failed after {_fmt(embed_time)} - req_id={req_id}: {e}",
						 exc_info=True)
			try:
				await interaction.followup.send("Couldn't render the stats right now.", ephemeral=True)
			except Exception as resp_error:
				logger.error(f"{LOG_PREFIX} Failed to send embed error response: {resp_error}")
			return

		# Add "View All Emojis" button if there are many favorites
		favorites = stats.get("favorites", {}) or {}
		if len(favorites) > 5:
			logger.debug(f"{LOG_PREFIX} Adding 'View All Emojis' button ({len(favorites)} favorites) - req_id={req_id}")
			try:
				view.add_item(
					discord.ui.Button(
						label="View All Emojis",
						style=discord.ButtonStyle.primary, # type: ignore
						custom_id=f"view_all_emojis_{target_member.id}",
						row=1
					)
				)
				logger.debug(f"{LOG_PREFIX} 'View All Emojis' button added successfully - req_id={req_id}")
			except Exception as button_error:
				logger.warning(f"{LOG_PREFIX} Failed to add 'View All Emojis' button - req_id={req_id}: {button_error}")
		# Continue without the button rather than failing

		logger.debug(f"{LOG_PREFIX} Sending final response - req_id={req_id}")
		t_send = _now()
		try:
			await asyncio.wait_for(
				interaction.followup.send(embed=embed, view=view, ephemeral=not public),
				timeout=SEND_TIMEOUT_SECONDS
			)
			send_time = _now() - t_send
			total_time = _now() - start_time
			logger.info(
				f"{LOG_PREFIX} Stats command completed successfully in {_fmt(total_time)} (send: {_fmt(send_time)}) - req_id={req_id}")

		except asyncio.TimeoutError:
			send_time = _now() - t_send
			total_time = _now() - start_time
			logger.warning(
				f"{LOG_PREFIX} Final send timeout after {_fmt(send_time)} (total: {_fmt(total_time)}) - req_id={req_id}")
			try:
				await interaction.followup.send("Stats are ready, but sending timed out. Please try again.",
												ephemeral=True)
			except Exception as resp_error:
				logger.error(f"{LOG_PREFIX} Failed to send final timeout notice: {resp_error}")

		except Exception as e:
			send_time = _now() - t_send if 't_send' in locals() else 0
			total_time = _now() - start_time
			logger.error(
				f"{LOG_PREFIX} Final send failed after {_fmt(send_time)} (total: {_fmt(total_time)}) - req_id={req_id}: {e}",
				exc_info=True)
			try:
				await interaction.followup.send("Couldn't send the stats message.", ephemeral=True)
			except Exception as resp_error:
				logger.error(f"{LOG_PREFIX} Failed to send final error notice: {resp_error}")


class UserStats(commands.Cog):
	"""
    A cog that handles user stats functionality and emoji view interactions.
    """

	def __init__(self, bot):
		self.bot = bot
		logger.info(f"{LOG_PREFIX} UserStats cog initializing")

	@commands.Cog.listener()
	async def on_interaction(self, interaction: discord.Interaction):
		"""Handles the button interaction for viewing all favorite emojis."""
		custom_id = str(interaction.data.get("custom_id", "")) if getattr(interaction, "data", None) else ""
		if interaction.type == discord.InteractionType.component and custom_id.startswith("view_all_emojis_"):
			start_time = _now()
			user_id = str(custom_id.split("_")[-1])

			logger.info(
				f"{LOG_PREFIX} 'View All Emojis' interaction triggered - guild={getattr(interaction.guild, 'id', None)}, "
				f"clicker={getattr(interaction.user, 'id', None)}, target_user={user_id}"
			)

			if not interaction.guild:
				logger.warning(f"{LOG_PREFIX} Emoji view rejected - no guild context")
				try:
					await interaction.response.send_message("This action is only available in a server.", # type: ignore
															ephemeral=True)
				except Exception as resp_error:
					logger.error(f"{LOG_PREFIX} Failed to send no-guild response: {resp_error}")
				return

			guild_id = str(interaction.guild.id)

			try:
				logger.debug(f"{LOG_PREFIX} Fetching emoji stats for user={user_id}")
				t0 = _now()

				# Use DatabaseManager to fetch emoji stats
				user_stats_collection = db_manager.get_raw_collection_from_connection('Ecom-Server', 'Users',
																					  'secondary')
				logger.debug(f"Collection fetched: {user_stats_collection} ({user_stats_collection.name})")
				stats = await asyncio.wait_for(
					user_stats_collection.find_one({
						"guild_id": guild_id,
						"user_id": user_id
					}),
					timeout=FETCH_TIMEOUT_SECONDS
				)

				fetch_time = _now() - t0
				stats_size = len(str(stats)) if stats else 0
				logger.info(
					f"{LOG_PREFIX} Emoji stats fetched in {_fmt(fetch_time)} - user={user_id}, size={stats_size} bytes")

			except asyncio.TimeoutError:
				fetch_time = _now() - t0
				logger.warning(f"{LOG_PREFIX} Emoji stats fetch timeout after {_fmt(fetch_time)} - user={user_id}")
				try:
					if interaction.response.is_done(): # type: ignore
						await interaction.followup.send("Fetching emoji stats timed out. Please try again.",
														ephemeral=True)
					else:
						await interaction.response.send_message("Fetching emoji stats timed out. Please try again.", #type: ignore
																ephemeral=True)
				except Exception as resp_error:
					logger.error(f"{LOG_PREFIX} Failed to send emoji timeout response: {resp_error}")
				return

			except Exception as e:
				fetch_time = _now() - t0 if 't0' in locals() else 0
				logger.error(f"{LOG_PREFIX} Emoji stats fetch failed after {_fmt(fetch_time)} - user={user_id}: {e}",
							 exc_info=True)
				try:
					error_msg = "⚠️ Unable to fetch emoji stats at the moment. Please try again later."
					if interaction.response.is_done(): # type: ignore
						await interaction.followup.send(error_msg, ephemeral=True)
					else:
						await interaction.response.send_message(error_msg, ephemeral=True) # type: ignore
				except Exception as resp_error:
					logger.error(f"{LOG_PREFIX} Failed to send emoji error response: {resp_error}")
				return

			if not stats:
				logger.info(f"{LOG_PREFIX} No emoji stats found for user={user_id}")
				try:
					no_stats_msg = "❌ No stats found for this user."
					if interaction.response.is_done(): # type: ignore
						await interaction.followup.send(no_stats_msg, ephemeral=True)
					else:
						await interaction.response.send_message(no_stats_msg, ephemeral=True) # type: ignore
				except Exception as resp_error:
					logger.error(f"{LOG_PREFIX} Failed to send no emoji stats response: {resp_error}")
				return

			favorites = stats.get("favorites", {}) or {}
			if not favorites:
				logger.info(f"{LOG_PREFIX} No favorite emojis for user={user_id}")
				try:
					no_emojis_msg = "⭐ No favorite emojis recorded for this user."
					if interaction.response.is_done(): # type: ignore
						await interaction.followup.send(no_emojis_msg, ephemeral=True)
					else:
						await interaction.response.send_message(no_emojis_msg, ephemeral=True) # type: ignore
				except Exception as resp_error:
					logger.error(f"{LOG_PREFIX} Failed to send no emojis response: {resp_error}")
				return

			logger.debug(f"{LOG_PREFIX} Processing {len(favorites)} favorite emojis for user={user_id}")
			process_start = _now()

			sorted_favorites = sorted(favorites.items(), key=lambda item: item[1], reverse=True)
			full_emojis_list = "\n".join(f"{emoji}: {count}" for emoji, count in sorted_favorites)
			emoji_pages = [full_emojis_list[i:i + 1900] for i in range(0, len(full_emojis_list), 1900)]

			process_time = _now() - process_start
			logger.debug(
				f"{LOG_PREFIX} Processed emoji list in {_fmt(process_time)} - {len(emoji_pages)} pages for user={user_id}")

			# Send first page via initial response (if available), rest via followups
			try:
				send_start = _now()
				if not interaction.response.is_done(): # type: ignore
					await asyncio.wait_for(
						interaction.response.send_message(f"⭐ **Full List of Emojis:**\n{emoji_pages[0]}", # type: ignore
														  ephemeral=True),
						timeout=SEND_TIMEOUT_SECONDS
					)
					for i, page in enumerate(emoji_pages[1:], 1):
						await interaction.followup.send(page, ephemeral=True)
						logger.debug(f"{LOG_PREFIX} Sent emoji page {i + 1}/{len(emoji_pages)} for user={user_id}")
				else:
					for idx, page in enumerate(emoji_pages):
						prefix = "⭐ **Full List of Emojis:**\n" if idx == 0 else ""
						await interaction.followup.send(f"{prefix}{page}", ephemeral=True)
						logger.debug(f"{LOG_PREFIX} Sent emoji page {idx + 1}/{len(emoji_pages)} for user={user_id}")

				send_time = _now() - send_start
				total_time = _now() - start_time
				logger.info(
					f"{LOG_PREFIX} Emoji list sent successfully in {_fmt(total_time)} (send: {_fmt(send_time)}) - {len(emoji_pages)} pages for user={user_id}")

			except asyncio.TimeoutError:
				send_time = _now() - send_start if 'send_start' in locals() else 0
				total_time = _now() - start_time
				logger.warning(
					f"{LOG_PREFIX} Emoji list send timeout after {_fmt(send_time)} (total: {_fmt(total_time)}) - user={user_id}")

			except Exception as e:
				send_time = _now() - send_start if 'send_start' in locals() else 0
				total_time = _now() - start_time
				logger.error(
					f"{LOG_PREFIX} Emoji list send failed after {_fmt(send_time)} (total: {_fmt(total_time)}) - user={user_id}: {e}",
					exc_info=True)


async def setup(bot: commands.Bot):
	"""Function used to load both cogs."""
	logger.info(f"{LOG_PREFIX} Starting cog setup")
	start_time = _now()

	try:
		await bot.add_cog(UserStats(bot))
		logger.info(f"{LOG_PREFIX} UserStats cog added successfully")

		await bot.add_cog(MemberCommands(bot))
		logger.info(f"{LOG_PREFIX} MemberCommands cog added successfully")

		setup_time = _now() - start_time
		logger.info(f"{LOG_PREFIX} Cog setup completed successfully in {_fmt(setup_time)}")

	except Exception as e:
		setup_time = _now() - start_time
		logger.error(f"{LOG_PREFIX} Cog setup failed after {_fmt(setup_time)}: {e}", exc_info=True)
		raise