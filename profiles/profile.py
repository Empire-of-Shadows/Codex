import os
from io import BytesIO
import asyncio
from typing import Dict, Optional, Tuple
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont
import pendulum
from dotenv import load_dotenv
from pymongo import IndexModel

from Database.DatabaseManager import db_manager
from utils.logger import get_logger, PerformanceLogger

# Load environment variables
load_dotenv()

logger = get_logger("Profile")
FONT_PATH = "fonts/Bebas_Neue/BebasNeue-Regular.ttf"  # Ensure this path exists

THEMES = ["dark", "neon", "gold", "minimal"]
LAYOUTS = ["detailed", "compact"]

# Global caches for performance
_font_cache: Dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}
_emoji_font_cache: Optional[ImageFont.FreeTypeFont] = None
_session: Optional[aiohttp.ClientSession] = None

# Pre-computed theme palettes for faster access
_DEFAULT_PALETTES = {
	"dark": {"bg": (34, 36, 40, 255), "accent": (88, 101, 242, 255), "text": (255, 255, 255, 255)},
	"neon": {"bg": (10, 12, 16, 255), "accent": (57, 255, 20, 255), "text": (230, 255, 230, 255)},
	"gold": {"bg": (30, 28, 24, 255), "accent": (212, 175, 55, 255), "text": (255, 245, 225, 255)},
	"minimal": {"bg": (245, 246, 250, 255), "accent": (64, 64, 64, 255), "text": (16, 16, 16, 255)},
}


def _get_session() -> aiohttp.ClientSession:
	"""Get or create the global aiohttp session."""
	global _session
	if _session is None or _session.closed:
		logger.debug("Creating new aiohttp session")
		connector = aiohttp.TCPConnector(
			limit=100,
			limit_per_host=30,
			ttl_dns_cache=300,
			use_dns_cache=True,
		)
		_session = aiohttp.ClientSession(
			connector=connector,
			timeout=aiohttp.ClientTimeout(total=5, connect=2)
		)
		logger.info("HTTP session created with optimized settings")
	return _session


def _calculate_xp_progress(level: int, current_xp: int) -> tuple[int, int]:
	"""
    Calculate XP progress using the actual leveling system formula.
    Returns (xp_in_current_level, xp_needed_for_next_level)
    """
	logger.debug(f"Calculating XP progress for level {level}, current XP {current_xp}")

	# Using the same formula as leveling.py: 50 * level * (level + 1)
	current_level_total_xp = 50 * level * (level + 1)
	next_level_total_xp = 50 * (level + 1) * (level + 2)

	xp_needed_for_next = next_level_total_xp - current_level_total_xp
	xp_progress_in_level = max(0, current_xp - current_level_total_xp)

	logger.debug(f"XP progress: {xp_progress_in_level}/{xp_needed_for_next}")
	return xp_progress_in_level, xp_needed_for_next


def _fmt_voice(seconds: float) -> str:
	"""Fast voice time formatting."""
	if seconds <= 0:
		return "0:00:00"

	total_seconds = int(seconds)
	h, remainder = divmod(total_seconds, 3600)
	m, s = divmod(remainder, 60)
	formatted = f"{h}:{m:02d}:{s:02d}"
	logger.debug(f"Formatted {seconds}s as {formatted}")
	return formatted


def _validate_hex_color(color_str: str) -> tuple[bool, tuple[int, int, int, int] | None]:
	"""Optimized hex color validation."""
	if not color_str or len(color_str) != 7 or color_str[0] != '#':
		logger.debug(f"Invalid hex color format: {color_str}")
		return False, None

	try:
		r = int(color_str[1:3], 16)
		g = int(color_str[3:5], 16)
		b = int(color_str[5:7], 16)
		logger.debug(f"Validated hex color {color_str} -> RGB({r}, {g}, {b})")
		return True, (r, g, b, 255)
	except ValueError as e:
		logger.warning(f"Failed to parse hex color {color_str}: {e}")
		return False, None


def _ensure_color_tuple(color_value) -> tuple[int, int, int, int]:
	"""Ensure color value is a proper RGBA tuple."""
	if isinstance(color_value, (list, tuple)) and len(color_value) >= 3:
		r, g, b = int(color_value[0]), int(color_value[1]), int(color_value[2])
		a = int(color_value[3]) if len(color_value) > 3 else 255
		result = (r, g, b, a)
		logger.debug(f"Ensured color tuple: {color_value} -> {result}")
		return result

	logger.debug(f"Invalid color value {color_value}, using default")
	return (34, 36, 40, 255)  # Default dark background


def _load_font_cached(path: str, size: int) -> ImageFont.FreeTypeFont:
	"""Load and cache fonts to avoid repeated disk I/O."""
	cache_key = (path, size)
	if cache_key not in _font_cache:
		logger.debug(f"Loading font: {path} size {size}")
		try:
			_font_cache[cache_key] = ImageFont.truetype(path, size)
			logger.info(f"Successfully loaded font: {path} size {size}")
		except Exception as e:
			logger.warning(f"Failed to load font {path} size {size}: {e}. Fallback to default PIL font")
			_font_cache[cache_key] = ImageFont.load_default()
	return _font_cache[cache_key]


def _load_emoji_font_cached(size: int = 22) -> ImageFont.FreeTypeFont:
	"""Load and cache emoji font."""
	global _emoji_font_cache
	if _emoji_font_cache is None:
		logger.debug(f"Loading emoji font, size {size}")
		emoji_font_paths = [
			"C:/Windows/Fonts/seguiemj.ttf",
			"C:/Windows/Fonts/NotoColorEmoji.ttf",
			"/System/Library/Fonts/Apple Color Emoji.ttc",
			"/Library/Fonts/Apple Color Emoji.ttc",
			"/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
			"/usr/share/fonts/TTF/NotoColorEmoji.ttf",
		]

		for font_path in emoji_font_paths:
			try:
				if os.path.exists(font_path):
					_emoji_font_cache = ImageFont.truetype(font_path, size)
					logger.info(f"Successfully loaded emoji font: {font_path}")
					break
			except Exception as e:
				logger.debug(f"Failed to load emoji font {font_path}: {e}")
				continue

		if _emoji_font_cache is None:
			logger.warning("No emoji font found, using default PIL font")
			_emoji_font_cache = ImageFont.load_default()

	return _emoji_font_cache


class ProfilePreferences:
	def __init__(self):
		"""Initialize ProfilePreferences with DatabaseManager integration."""
		logger.info("ProfilePreferences initialized with DatabaseManager")
		# Register collection configurations if not already present
		self._ensure_collection_configs()

	def _ensure_collection_configs(self):
		"""Ensure ProfileCard collections are configured in DatabaseManager."""
		# Add ProfileCard collections configuration
		if 'profilecard_preferences' not in db_manager._collection_configs:
			from Database.DatabaseManager import CollectionConfig

			db_manager._collection_configs['profilecard_preferences'] = CollectionConfig(
				name='ProfilePreferences',
				database='ProfileCard',
				connection='primary',
				indexes=[
					IndexModel([('user_id', 1), ('guild_id', 1)], unique=True, background=True),
					IndexModel([('updated_at', -1)], background=True)
				]
			)

		if 'profilecard_themes' not in db_manager._collection_configs:
			from Database.DatabaseManager import CollectionConfig

			db_manager._collection_configs['profilecard_themes'] = CollectionConfig(
				name='CustomThemes',
				database='ProfileCard',
				connection='primary',
				indexes=[
					IndexModel([('user_id', 1), ('guild_id', 1)], background=True),
					IndexModel([('user_id', 1), ('guild_id', 1), ('theme_name', 1)], unique=True, background=True),
					IndexModel([('created_at', -1)], background=True)
				]
			)

	@property
	def preferences(self):
		"""Get ProfilePreferences collection manager."""
		return db_manager.get_collection_manager('profilecard_preferences')

	@property
	def custom_themes(self):
		"""Get CustomThemes collection manager."""
		return db_manager.get_collection_manager('profilecard_themes')

	async def get_user_preferences_and_theme(self, user_id: str, guild_id: str, theme_name: str) -> tuple[dict, dict]:
		"""Combined query to get both preferences and theme palette in one operation."""
		with PerformanceLogger(logger, f"get_user_preferences_and_theme for user {user_id}"):
			logger.debug(f"Fetching preferences and theme for user {user_id}, guild {guild_id}, theme {theme_name}")

			# Use aggregation pipeline for efficient combined query
			pipeline = [
				{
					"$facet": {
						"preferences": [
							{"$match": {"user_id": user_id, "guild_id": guild_id}},
							{"$limit": 1}
						],
						"custom_theme": [
							{"$lookup": {
								"from": "CustomThemes",
								"let": {"uid": user_id, "gid": guild_id, "theme": theme_name},
								"pipeline": [
									{"$match": {
										"$expr": {
											"$and": [
												{"$eq": ["$user_id", "$$uid"]},
												{"$eq": ["$guild_id", "$$gid"]},
												{"$eq": ["$theme_name", "$$theme"]}
											]
										}
									}}
								],
								"as": "theme_data"
							}},
							{"$limit": 1}
						]
					}
				}
			]

			try:
				result = await self.preferences.aggregate(pipeline)
				if result:
					data = result[0]
					logger.debug(
						f"Combined query returned data: preferences={bool(data.get('preferences'))}, custom_theme={bool(data.get('custom_theme'))}")

					# Process preferences
					prefs_data = data.get("preferences", [])
					preferences = {
						"theme": "dark",
						"layout": "detailed",
						"show_inventory": True,
						"show_badges": True
					}
					if prefs_data:
						prefs = prefs_data[0]
						for key in preferences:
							if key in prefs:
								preferences[key] = prefs[key]
						logger.debug(f"Loaded user preferences: {preferences}")

					# Process theme palette
					theme_palette = _DEFAULT_PALETTES.get(theme_name, _DEFAULT_PALETTES["dark"])

					custom_theme_data = data.get("custom_theme", [])
					if custom_theme_data and custom_theme_data[0].get("theme_data"):
						theme_doc = custom_theme_data[0]["theme_data"][0]
						if "colors" in theme_doc:
							colors = theme_doc["colors"]
							theme_palette = {
								"bg": _ensure_color_tuple(colors.get("bg", [34, 36, 40, 255])),
								"accent": _ensure_color_tuple(colors.get("accent", [88, 101, 242, 255])),
								"text": _ensure_color_tuple(colors.get("text", [255, 255, 255, 255]))
							}
							logger.info(f"Loaded custom theme '{theme_name}' for user {user_id}")

					return preferences, theme_palette

			except Exception as e:
				logger.error(f"Combined query failed: {e}")

			# Fallback to default values
			logger.debug("Using default preferences and theme palette")
			return {
				"theme": "dark", "layout": "detailed",
				"show_inventory": True, "show_badges": True
			}, _DEFAULT_PALETTES["dark"]

	async def get_user_preferences(self, user_id: str, guild_id: str) -> dict:
		"""Get user's saved profile preferences."""
		with PerformanceLogger(logger, f"get_user_preferences for user {user_id}"):
			logger.debug(f"Fetching preferences for user {user_id}, guild {guild_id}")
			try:
				prefs = await self.preferences.find_one(
					{"user_id": user_id, "guild_id": guild_id},
					{"_id": 0}  # Exclude _id for faster transfer
				)
				logger.debug(f"Retrieved preferences: {bool(prefs)}")
			except Exception as e:
				logger.error(f"Failed to fetch user preferences: {e}")
				prefs = None

			defaults = {
				"theme": "dark",
				"layout": "detailed",
				"show_inventory": True,
				"show_badges": True
			}

			if prefs:
				for key in defaults:
					if key in prefs:
						defaults[key] = prefs[key]
				logger.info(f"Loaded preferences for user {user_id}: {defaults}")
			else:
				logger.debug(f"No saved preferences found for user {user_id}, using defaults")

			return defaults

	async def save_user_preferences(self, user_id: str, guild_id: str, preferences: dict):
		"""Save user's profile preferences."""
		with PerformanceLogger(logger, f"save_user_preferences for user {user_id}"):
			logger.info(f"Saving preferences for user {user_id}: {preferences}")
			try:
				await self.preferences.update_one(
					{"user_id": user_id, "guild_id": guild_id},
					{
						"$set": {
							"user_id": user_id,
							"guild_id": guild_id,
							**preferences,
							"updated_at": pendulum.now().isoformat()
						}
					},
					upsert=True
				)
				logger.info(f"Successfully saved preferences for user {user_id}")
			except Exception as e:
				logger.error(f"Failed to save preferences for user {user_id}: {e}")
				raise

	async def get_theme_palette(self, theme_name: str, user_id: str, guild_id: str) -> dict:
		"""Get theme palette, checking custom themes first."""
		logger.debug(f"Getting theme palette: {theme_name} for user {user_id}")

		if theme_name in _DEFAULT_PALETTES:
			logger.debug(f"Using default palette for theme {theme_name}")
			return _DEFAULT_PALETTES[theme_name]

		with PerformanceLogger(logger, f"get_custom_theme_palette {theme_name}"):
			try:
				custom_theme = await self.custom_themes.find_one(
					{
						"user_id": user_id,
						"guild_id": guild_id,
						"theme_name": theme_name
					},
					{"colors": 1, "_id": 0}  # Only get colors field
				)

				if custom_theme and "colors" in custom_theme:
					colors = custom_theme["colors"]
					palette = {
						"bg": _ensure_color_tuple(colors.get("bg", [34, 36, 40, 255])),
						"accent": _ensure_color_tuple(colors.get("accent", [88, 101, 242, 255])),
						"text": _ensure_color_tuple(colors.get("text", [255, 255, 255, 255]))
					}
					logger.info(f"Retrieved custom theme palette for {theme_name}")
					return palette

			except Exception as e:
				logger.error(f"Failed to fetch custom theme {theme_name}: {e}")

		logger.debug(f"Custom theme {theme_name} not found, using default dark palette")
		return _DEFAULT_PALETTES["dark"]

	async def get_available_themes(self, user_id: str, guild_id: str) -> list[str]:
		"""Get list of available themes including custom ones."""
		with PerformanceLogger(logger, f"get_available_themes for user {user_id}"):
			themes = list(THEMES)
			logger.debug(f"Starting with default themes: {themes}")

			try:
				custom_theme_docs = await self.custom_themes.find_many(
					{"user_id": user_id, "guild_id": guild_id},
					{"theme_name": 1, "_id": 0},
					limit=20
				)

				custom_themes = [doc["theme_name"] for doc in custom_theme_docs]
				themes.extend(custom_themes)
				logger.info(f"Found {len(custom_themes)} custom themes for user {user_id}: {custom_themes}")

			except Exception as e:
				logger.error(f"Failed to fetch custom themes for user {user_id}: {e}")

			logger.debug(f"Total available themes for user {user_id}: {len(themes)}")
			return themes

	async def save_custom_theme(self, user_id: str, guild_id: str, theme_name: str, colors: dict):
		"""Save a custom theme."""
		with PerformanceLogger(logger, f"save_custom_theme {theme_name} for user {user_id}"):
			logger.info(f"Saving custom theme '{theme_name}' for user {user_id}")
			logger.debug(f"Theme colors: {colors}")

			custom_theme = {
				"user_id": user_id,
				"guild_id": guild_id,
				"theme_name": theme_name,
				"colors": colors,
				"created_at": pendulum.now().isoformat()
			}

			try:
				await self.custom_themes.update_one(
					{
						"user_id": user_id,
						"guild_id": guild_id,
						"theme_name": theme_name
					},
					{"$set": custom_theme},
					upsert=True
				)
				logger.info(f"Successfully saved custom theme '{theme_name}' for user {user_id}")
			except Exception as e:
				logger.error(f"Failed to save custom theme '{theme_name}' for user {user_id}: {e}")
				raise

	async def delete_custom_theme(self, user_id: str, guild_id: str, theme_name: str) -> bool:
		"""Delete a custom theme. Returns True if deleted, False if not found."""
		with PerformanceLogger(logger, f"delete_custom_theme {theme_name} for user {user_id}"):
			logger.info(f"Deleting custom theme '{theme_name}' for user {user_id}")

			try:
				deleted = await self.custom_themes.delete_one({
					"user_id": user_id,
					"guild_id": guild_id,
					"theme_name": theme_name
				})

				if deleted:
					logger.info(f"Successfully deleted custom theme '{theme_name}' for user {user_id}")
				else:
					logger.warning(f"Custom theme '{theme_name}' not found for user {user_id}")

				return deleted

			except Exception as e:
				logger.error(f"Failed to delete custom theme '{theme_name}' for user {user_id}: {e}")
				raise


class ProfileCardView(discord.ui.View):
	def __init__(
			self,
			user_data: dict,  # Pre-computed user data
			theme_palette: dict,  # Pre-computed theme palette
			theme: str,
			layout: str,
			show_inventory: bool,
			show_badges: bool,
			public: bool,
			preferences: ProfilePreferences,
	):
		super().__init__(timeout=120)
		self.user_data = user_data
		self.theme_palette = theme_palette
		self.theme = theme
		self.available_themes = []
		self.layout = layout if layout in LAYOUTS else "detailed"
		self.show_inventory = show_inventory
		self.show_badges = show_badges
		self.public = public
		self.preferences = preferences

		logger.debug(f"ProfileCardView initialized with theme={theme}, layout={layout}, public={public}")

	async def _send_card(self, interaction: discord.Interaction):
		"""Generate and send the profile card."""
		with PerformanceLogger(logger, f"send_profile_card for user {interaction.user.id}"):
			logger.debug(f"Generating profile card for {interaction.user.id} with theme {self.theme}")

			# Get updated theme palette if theme changed
			if self.theme not in _DEFAULT_PALETTES:
				theme_palette = await self.preferences.get_theme_palette(
					self.theme,
					str(interaction.user.id),
					str(interaction.guild.id)
				)
			else:
				theme_palette = _DEFAULT_PALETTES[self.theme]

			image = await create_profile_card(
				user_data=self.user_data,
				theme_palette=theme_palette,
				layout=self.layout,
				show_inventory=self.show_inventory,
				show_badges=self.show_badges,
			)

			buf = BytesIO()
			image.save(buf, format="PNG", optimize=True)
			buf.seek(0)
			file = discord.File(buf, filename=f"profile_card_{interaction.user.id}.png")

			logger.info(f"Profile card generated and sent for user {interaction.user.id}")
			await interaction.followup.send(file=file, ephemeral=not self.public)

	@discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, row=0)
	async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
		logger.info(f"Profile card refresh requested by user {interaction.user.id}")
		try:
			if not interaction.response.is_done():
				await interaction.response.defer(ephemeral=not self.public)
			await self._send_card(interaction)
		except Exception as e:
			logger.exception(f"Failed to refresh profile card for user {interaction.user.id}: {e}")
			await interaction.followup.send("Couldn't refresh the card right now.", ephemeral=True)

	@discord.ui.button(label="Theme ◀", style=discord.ButtonStyle.primary, row=0)
	async def theme_prev(self, interaction: discord.Interaction, button: discord.ui.Button):
		logger.debug(f"Theme previous requested by user {interaction.user.id}")
		try:
			if not interaction.response.is_done():
				await interaction.response.defer(ephemeral=not self.public)

			if not self.available_themes:
				self.available_themes = await self.preferences.get_available_themes(
					str(interaction.user.id), str(interaction.guild.id)
				)

			try:
				idx = self.available_themes.index(self.theme)
			except ValueError:
				idx = 0

			old_theme = self.theme
			self.theme = self.available_themes[(idx - 1) % len(self.available_themes)]
			logger.info(f"User {interaction.user.id} switched theme: {old_theme} -> {self.theme}")
			await self._send_card(interaction)
		except Exception as e:
			logger.exception(f"theme_prev failed for user {interaction.user.id}: {e}")

	@discord.ui.button(label="Theme ▶", style=discord.ButtonStyle.primary, row=0)
	async def theme_next(self, interaction: discord.Interaction, button: discord.ui.Button):
		logger.debug(f"Theme next requested by user {interaction.user.id}")
		try:
			if not interaction.response.is_done():
				await interaction.response.defer(ephemeral=not self.public)

			if not self.available_themes:
				self.available_themes = await self.preferences.get_available_themes(
					str(interaction.user.id), str(interaction.guild.id)
				)

			try:
				idx = self.available_themes.index(self.theme)
			except ValueError:
				idx = 0

			old_theme = self.theme
			self.theme = self.available_themes[(idx + 1) % len(self.available_themes)]
			logger.info(f"User {interaction.user.id} switched theme: {old_theme} -> {self.theme}")
			await self._send_card(interaction)
		except Exception as e:
			logger.exception(f"theme_next failed for user {interaction.user.id}: {e}")

	@discord.ui.button(label="Layout", style=discord.ButtonStyle.secondary, row=0)
	async def toggle_layout(self, interaction: discord.Interaction, button: discord.ui.Button):
		logger.debug(f"Layout toggle requested by user {interaction.user.id}")
		try:
			if not interaction.response.is_done():
				await interaction.response.defer(ephemeral=not self.public)
			old_layout = self.layout
			self.layout = "compact" if self.layout == "detailed" else "detailed"
			logger.info(f"User {interaction.user.id} switched layout: {old_layout} -> {self.layout}")
			await self._send_card(interaction)
		except Exception as e:
			logger.exception(f"toggle_layout failed for user {interaction.user.id}: {e}")

	@discord.ui.button(label="Share Publicly", style=discord.ButtonStyle.success, row=1)
	async def share_public(self, interaction: discord.Interaction, button: discord.ui.Button):
		logger.info(f"Public sharing requested by user {interaction.user.id}")
		try:
			if not interaction.response.is_done():
				await interaction.response.defer(ephemeral=False)
			self.public = True
			await self._send_card(interaction)
		except Exception as e:
			logger.exception(f"share_public failed for user {interaction.user.id}: {e}")

	@discord.ui.button(label="Toggle Badges", style=discord.ButtonStyle.secondary, row=1)
	async def toggle_badges(self, interaction: discord.Interaction, button: discord.ui.Button):
		logger.debug(f"Badges toggle requested by user {interaction.user.id}")
		try:
			if not interaction.response.is_done():
				await interaction.response.defer(ephemeral=not self.public)
			self.show_badges = not self.show_badges
			logger.info(f"User {interaction.user.id} toggled badges: {self.show_badges}")
			await self._send_card(interaction)
		except Exception as e:
			logger.exception(f"toggle_badges failed for user {interaction.user.id}: {e}")

	@discord.ui.button(label="Toggle Inventory", style=discord.ButtonStyle.secondary, row=1)
	async def toggle_inventory(self, interaction: discord.Interaction, button: discord.ui.Button):
		logger.debug(f"Inventory toggle requested by user {interaction.user.id}")
		try:
			if not interaction.response.is_done():
				await interaction.response.defer(ephemeral=not self.public)
			self.show_inventory = not self.show_inventory
			logger.info(f"User {interaction.user.id} toggled inventory: {self.show_inventory}")
			await self._send_card(interaction)
		except Exception as e:
			logger.exception(f"toggle_inventory failed for user {interaction.user.id}: {e}")

	@discord.ui.button(label="💾 Save Settings", style=discord.ButtonStyle.secondary, row=2)
	async def save_preferences(self, interaction: discord.Interaction, button: discord.ui.Button):
		"""Save current settings as user's default preferences."""
		logger.info(f"Preferences save requested by user {interaction.user.id}")
		try:
			if not interaction.response.is_done():
				await interaction.response.defer(ephemeral=True)

			preferences = {
				"theme": self.theme,
				"layout": self.layout,
				"show_inventory": self.show_inventory,
				"show_badges": self.show_badges
			}

			await self.preferences.save_user_preferences(
				str(interaction.user.id),
				str(interaction.guild.id),
				preferences
			)

			logger.info(f"Preferences saved for user {interaction.user.id}: {preferences}")
			await interaction.followup.send(
				f"✅ Profile settings saved!\n"
				f"• Theme: **{self.theme.title()}**\n"
				f"• Layout: **{self.layout.title()}**\n"
				f"• Show Inventory: **{self.show_inventory}**\n"
				f"• Show Badges: **{self.show_badges}**",
				ephemeral=True
			)
		except Exception as e:
			logger.exception(f"Failed to save preferences for user {interaction.user.id}: {e}")
			await interaction.followup.send(
				"❌ Failed to save preferences. Please try again later.",
				ephemeral=True
			)


class Profile(commands.Cog):
	"""A cog for profiles-related slash commands."""

	def __init__(self, bot: commands.Bot):
		self.bot = bot
		self.preferences = None
		logger.info("Profile cog initialized")

	async def cog_load(self):
		"""Run when the cog is loaded."""
		logger.info("Loading Profile cog...")
		with PerformanceLogger(logger, "Profile cog loading"):
			await db_manager.initialize()
			self.preferences = ProfilePreferences()
			logger.info("Profile cog loaded successfully")

	async def cog_unload(self):
		"""Clean up resources when cog is unloaded."""
		logger.info("Unloading Profile cog...")
		global _session
		if _session and not _session.closed:
			await _session.close()
			logger.info("HTTP session closed")
		logger.info("Profile cog unloaded")

	async def _fetch_user_data(self, user: discord.User, guild: discord.Guild) -> dict:
		"""Efficiently fetch all user data in parallel."""
		with PerformanceLogger(logger, f"fetch_user_data for {user.id}"):
			user_id_str = str(user.id)
			guild_id_str = str(guild.id)

			logger.debug(f"Fetching user data for {user.id} in guild {guild.id}")

			# Use DatabaseManager for data fetching
			try:
				# Get ServerData collections through DatabaseManager
				users_manager = db_manager.get_collection_manager('serverdata_users')

				# For other collections not yet configured in DatabaseManager, use raw collection access
				user_stats_collection = db_manager.get_raw_collection('ServerData', 'UserStats')
				inventory_collection = db_manager.get_raw_collection('Inventory', 'Items')

				# Parallel database queries
				member_task = users_manager.find_one(
					{"guild_id": guild.id, "id": user.id},
					{"display_name": 1, "joined_at": 1, "avatar_url": 1, "_id": 0}
				)
				stats_task = user_stats_collection.find_one(
					{"guild_id": guild_id_str, "user_id": user_id_str},
					{
						"level": 1, "xp": 1, "embers": 1,
						"message_stats.messages": 1, "message_stats.daily_streak": 1,
						"voice_stats.voice_seconds": 1, "voice_stats.voice_sessions": 1,
						"favorites": 1, "_id": 0
					}
				)
				inventory_task = inventory_collection.count_documents({"user_id": user_id_str})

				# Await all tasks concurrently
				start_time = time.time()
				member_data, stats_doc, inv_count = await asyncio.gather(
					member_task, stats_task, inventory_task, return_exceptions=True
				)
				fetch_time = time.time() - start_time
				logger.debug(f"Database queries completed in {fetch_time:.3f}s")

			except Exception as e:
				logger.error(f"Failed to fetch user data from DatabaseManager: {e}")
				# Fallback to direct database access if needed
				member_data, stats_doc, inv_count = None, None, 0

			# Process member data
			if isinstance(member_data, Exception) or not member_data:
				logger.debug(f"No member data found for user {user.id}, using Discord fallbacks")
				nickname = user.name
				join_date = user.joined_at.strftime("MMMM DD, YYYY") if user.joined_at else "Unknown"
				avatar_url = user.display_avatar.url
			else:
				logger.debug(f"Member data retrieved for user {user.id}")
				nickname = member_data.get("display_name", user.name)
				join_date = member_data.get("joined_at", "Unknown")
				avatar_url = member_data.get("avatar_url", user.display_avatar.url)
				if join_date != "Unknown":
					try:
						join_date = pendulum.parse(join_date).format("MMMM DD, YYYY")
					except Exception as e:
						logger.warning(f"Failed to parse join date {join_date}: {e}")
						join_date = "Unknown"

			# Process stats data
			level = xp = embers = messages = streak = 0
			voice_seconds = 0.0
			voice_sessions = 0
			fav_tuple = None

			if not isinstance(stats_doc, Exception) and stats_doc:
				logger.debug(f"Stats data retrieved for user {user.id}")
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
						logger.debug(f"Favorite emoji for user {user.id}: {top_emoji} (×{top_count})")
					except Exception as e:
						logger.debug(f"Failed to process favorites for user {user.id}: {e}")
			else:
				logger.debug(f"No stats data found for user {user.id}, using defaults")

			# Process inventory count
			if isinstance(inv_count, Exception):
				logger.warning(f"Failed to get inventory count for user {user.id}: {inv_count}")
				inv_count = None
			else:
				logger.debug(f"Inventory count for user {user.id}: {inv_count}")

			# Calculate XP progress
			xp_progress, xp_needed = _calculate_xp_progress(level, xp)

			user_data = {
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

			logger.info(f"User data compiled for {user.id}: Level {level}, {messages} messages, {embers} embers")
			return user_data

	# Theme command group
	theme_group = app_commands.Group(name="theme", description="Manage profile card themes")

	@theme_group.command(name="create", description="Create a custom color theme for your profile card")
	@app_commands.describe(
		name="Name for your custom theme (2-20 characters)",
		background="Background color (hex format: #RRGGBB)",
		accent="Accent color (hex format: #RRGGBB)",
		text="Text color (hex format: #RRGGBB)"
	)
	async def create_theme(
			self,
			interaction: discord.Interaction,
			name: str,
			background: str,
			accent: str,
			text: str
	):
		"""Create a custom theme with user-defined colors."""
		logger.info(f"Custom theme creation requested by {interaction.user.id}: name={name}")
		await interaction.response.defer(ephemeral=True)

		# Check if user already has a custom theme
		try:
			existing_theme = await self.preferences.custom_themes.find_one(
				{
					"user_id": str(interaction.user.id),
					"guild_id": str(interaction.guild.id)
				},
				{"theme_name": 1, "_id": 0}
			)

			if existing_theme:
				existing_name = existing_theme.get("theme_name", "unknown")
				logger.info(f"User {interaction.user.id} already has custom theme '{existing_name}'")
				await interaction.followup.send(
					f"❌ You already have a custom theme called `{existing_name}`.\n"
					f"Please delete it first using `/theme delete theme_name:{existing_name}` before creating a new one.",
					ephemeral=True
				)
				return
		except Exception as e:
			logger.error(f"Failed to check existing themes for user {interaction.user.id}: {e}")
			await interaction.followup.send("❌ Failed to check existing themes. Please try again.", ephemeral=True)
			return

		# Validate theme name
		name = name.lower().strip()
		if len(name) < 2 or len(name) > 20:
			logger.warning(f"Invalid theme name length for user {interaction.user.id}: '{name}' ({len(name)} chars)")
			await interaction.followup.send("❌ Theme name must be between 2-20 characters.", ephemeral=True)
			return

		if not name.replace('_', '').replace('-', '').isalnum():
			logger.warning(f"Invalid theme name characters for user {interaction.user.id}: '{name}'")
			await interaction.followup.send("❌ Theme name can only contain letters, numbers, hyphens and underscores.",
											ephemeral=True)
			return

		# Prevent overwriting default themes
		if name in THEMES:
			logger.warning(f"User {interaction.user.id} tried to use default theme name: '{name}'")
			await interaction.followup.send(f"❌ Cannot use `{name}` as it's a default theme name.", ephemeral=True)
			return

		# Validate hex colors
		bg_valid, bg_color = _validate_hex_color(background)
		accent_valid, accent_color = _validate_hex_color(accent)
		text_valid, text_color = _validate_hex_color(text)

		if not all([bg_valid, accent_valid, text_valid]):
			logger.warning(
				f"Invalid hex colors for user {interaction.user.id}: bg={background}, accent={accent}, text={text}")
			await interaction.followup.send(
				"❌ Invalid color format. Please use hex format like `#FF0000` for red.\n"
				"Example: `/theme create name:sunset background:#2C1810 accent:#FF6B35 text:#FFFFFF`",
				ephemeral=True
			)
			return

		try:
			colors = {
				"bg": bg_color,
				"accent": accent_color,
				"text": text_color
			}

			await self.preferences.save_custom_theme(
				str(interaction.user.id),
				str(interaction.guild.id),
				name,
				colors
			)

			logger.info(f"Custom theme '{name}' created successfully for user {interaction.user.id}")
			await interaction.followup.send(
				f"✅ Custom theme `{name}` created successfully!\n"
				f"🎨 **Background:** {background}\n"
				f"🎨 **Accent:** {accent}\n"
				f"🎨 **Text:** {text}\n\n"
				f"Use `/member card` and cycle through themes with the arrow buttons to try it out!\n\n"
				f"💡 **Note:** You can only have one custom theme at a time. "
				f"Use `/theme delete` to remove this theme if you want to create a different one.",
				ephemeral=True
			)

		except Exception as e:
			logger.error(f"Failed to save custom theme for user {interaction.user.id}: {e}")
			await interaction.followup.send("❌ Failed to save custom theme. Please try again.", ephemeral=True)

	@theme_group.command(name="list", description="List all available themes including your custom ones")
	async def list_themes(self, interaction: discord.Interaction):
		"""List available themes."""
		logger.info(f"Theme list requested by user {interaction.user.id}")
		await interaction.response.defer(ephemeral=True)

		try:
			available_themes = await self.preferences.get_available_themes(
				str(interaction.user.id),
				str(interaction.guild.id)
			)

			default_themes = [t for t in available_themes if t in THEMES]
			custom_themes = [t for t in available_themes if t not in THEMES]

			logger.debug(
				f"Themes for user {interaction.user.id}: {len(default_themes)} default, {len(custom_themes)} custom")

			embed = discord.Embed(
				title="🎨 Available Themes",
				color=discord.Color.blue()
			)

			if default_themes:
				embed.add_field(
					name="Default Themes",
					value=", ".join(f"`{t}`" for t in default_themes),
					inline=False
				)

			if custom_themes:
				embed.add_field(
					name="Your Custom Themes",
					value=", ".join(f"`{t}`" for t in custom_themes),
					inline=False
				)
			else:
				embed.add_field(
					name="Your Custom Themes",
					value="None created yet. Use `/theme create` to make one!",
					inline=False
				)

			embed.set_footer(text="Use the theme navigation buttons in /member card to browse themes")
			await interaction.followup.send(embed=embed, ephemeral=True)

		except Exception as e:
			logger.error(f"Failed to list themes for user {interaction.user.id}: {e}")
			await interaction.followup.send("❌ Failed to load themes.", ephemeral=True)

	@theme_group.command(name="delete", description="Delete one of your custom themes")
	@app_commands.describe(theme_name="Name of the custom theme to delete")
	async def delete_theme(self, interaction: discord.Interaction, theme_name: str):
		"""Delete a custom theme."""
		logger.info(f"Theme deletion requested by user {interaction.user.id}: '{theme_name}'")
		await interaction.response.defer(ephemeral=True)

		theme_name = theme_name.lower().strip()

		# Prevent deletion of default themes
		if theme_name in THEMES:
			logger.warning(f"User {interaction.user.id} tried to delete default theme: '{theme_name}'")
			await interaction.followup.send("❌ Cannot delete default themes.", ephemeral=True)
			return

		try:
			deleted = await self.preferences.delete_custom_theme(
				str(interaction.user.id),
				str(interaction.guild.id),
				theme_name
			)

			if deleted:
				logger.info(f"Custom theme '{theme_name}' deleted for user {interaction.user.id}")
				await interaction.followup.send(f"✅ Custom theme `{theme_name}` deleted successfully.", ephemeral=True)
			else:
				logger.warning(f"Custom theme '{theme_name}' not found for user {interaction.user.id}")
				await interaction.followup.send(f"❌ Custom theme `{theme_name}` not found.", ephemeral=True)

		except Exception as e:
			logger.error(f"Failed to delete custom theme '{theme_name}' for user {interaction.user.id}: {e}")
			await interaction.followup.send("❌ Failed to delete theme.", ephemeral=True)


def _is_emoji_supported(text: str, font: ImageFont.FreeTypeFont) -> bool:
	"""Check if the font supports the given emoji/text."""
	try:
		bbox = font.getbbox(text)
		supported = bbox[2] > bbox[0] and bbox[3] > bbox[1]
		logger.debug(f"Emoji support check for '{text}': {supported}")
		return supported
	except Exception as e:
		logger.debug(f"Emoji support check failed for '{text}': {e}")
		return False


async def create_profile_card(
		user_data: dict,
		theme_palette: dict,
		layout: str = "detailed",
		show_inventory: bool = True,
		show_badges: bool = True,
) -> Image.Image:
	"""Optimized profile card generation."""
	with PerformanceLogger(logger, f"create_profile_card layout={layout}"):
		logger.debug(f"Creating profile card: layout={layout}, inventory={show_inventory}, badges={show_badges}")

		# Pre-calculate dimensions and positions
		card_width = 800
		card_height = 300 if layout == "detailed" else 240
		avatar_size = 200
		avatar_pos = (50, 40 if layout == "detailed" else 20)
		base_x = 300

		if layout == "detailed":
			positions = {
				"nickname": 50, "join_date": 95, "embers": 120,
				"messages": 142, "voice": 164, "progress_text": 192,
				"progress_bar": 216, "footer": card_height - 40
			}
		else:
			positions = {
				"nickname": 28, "join_date": 62, "embers": 84,
				"messages": 104, "voice": 124, "progress_text": 148,
				"progress_bar": 172, "footer": card_height - 32
			}

		logger.debug(f"Card dimensions: {card_width}x{card_height}, avatar: {avatar_size}px")

		# Create base image
		card = Image.new("RGBA", (card_width, card_height), theme_palette["bg"])
		draw = ImageDraw.Draw(card)

		# Load fonts from cache
		font_large = _load_font_cached(FONT_PATH, 40)
		font_small = _load_font_cached(FONT_PATH, 22)
		font_emoji = _load_emoji_font_cached(22)

		# Fetch avatar asynchronously with optimized session
		avatar_task = _fetch_avatar_optimized(user_data["avatar_url"], avatar_size)

		# Continue with text rendering while avatar loads
		nickname_disp = user_data["nickname"]
		try:
			max_w = 440
			while draw.textlength(nickname_disp, font=font_large) > max_w and len(nickname_disp) > 3:
				nickname_disp = nickname_disp[:-2]
			if nickname_disp != user_data["nickname"]:
				nickname_disp += "…"
				logger.debug(f"Truncated nickname: '{user_data['nickname']}' -> '{nickname_disp}'")
		except Exception as e:
			logger.warning(f"Nickname truncation failed: {e}")

		# Render text elements
		text_color = theme_palette["text"]
		logger.debug("Rendering text elements")

		draw.text((base_x, positions["nickname"]), nickname_disp, fill=text_color, font=font_large)
		draw.text((base_x, positions["join_date"]), f"Joined: {user_data['join_date']}", fill=text_color,
				  font=font_small)
		draw.text((base_x, positions["embers"]), f"Embers: {user_data['embers']:,}", fill=text_color, font=font_small)
		draw.text((base_x, positions["messages"]),
				  f"Messages: {user_data['messages']:,} • Streak: {user_data['streak']}d",
				  fill=text_color, font=font_small)
		draw.text((base_x, positions["voice"]),
				  f"Voice: {_fmt_voice(user_data['voice_seconds'])} • Sessions: {user_data['voice_sessions']}",
				  fill=text_color, font=font_small)

		# Progress bar
		try:
			pct = min(1.0, float(user_data["xp_progress"]) / max(1, user_data["xp_needed"]))
			bar_x, bar_w, bar_h = base_x, 440, 18
			progress_y = positions["progress_bar"]

			if progress_y + bar_h + 10 < positions["footer"]:
				draw.text((bar_x, positions["progress_text"]),
						  f"Level {user_data['level']} · {user_data['xp_progress']:,}/{user_data['xp_needed']:,}",
						  fill=text_color, font=font_small)
				draw.rounded_rectangle((bar_x, progress_y, bar_x + bar_w, progress_y + bar_h), radius=9,
									   fill=(255, 255, 255, 30))
				draw.rounded_rectangle((bar_x, progress_y, bar_x + int(bar_w * pct), progress_y + bar_h), radius=9,
									   fill=theme_palette["accent"])
				logger.debug(f"Progress bar rendered: {pct:.1%} ({user_data['xp_progress']}/{user_data['xp_needed']})")
		except Exception as e:
			logger.warning(f"Progress bar rendering failed: {e}")

		# Footer elements
		footer_y = positions["footer"]
		x_gap = 180

		# Favorite emoji
		if user_data["favorite"]:
			fav_emoji, fav_count = user_data["favorite"]
			logger.debug(f"Rendering favorite emoji: {fav_emoji} (×{fav_count})")
			try:
				if _is_emoji_supported(fav_emoji, font_emoji):
					draw.text((base_x, footer_y), "Fav: ", fill=text_color, font=font_small)
					fav_text_width = draw.textlength("Fav: ", font=font_small)
					draw.text((base_x + fav_text_width, footer_y), fav_emoji, fill=text_color, font=font_emoji)
					emoji_width = draw.textlength(fav_emoji, font=font_emoji)
					draw.text((base_x + fav_text_width + emoji_width, footer_y), f" × {fav_count}", fill=text_color,
							  font=font_small)
				else:
					draw.text((base_x, footer_y), f"Fav: ⭐ × {fav_count}", fill=text_color, font=font_small)
			except Exception as e:
				logger.warning(f"Favorite emoji rendering failed: {e}")
				draw.text((base_x, footer_y), f"Fav: ⭐ × {fav_count}", fill=text_color, font=font_small)

		# Badges and inventory
		if show_badges:
			draw.text((base_x + x_gap, footer_y), "Badges: ⭐ ⭐", fill=text_color, font=font_small)
			logger.debug("Badges section rendered")

		if show_inventory:
			inv_txt = f"Items: {user_data['inventory_count']}" if isinstance(user_data["inventory_count"],
																			 int) else "Items: —"
			inv_x_offset = x_gap * 2 if show_badges else x_gap
			draw.text((base_x + inv_x_offset, footer_y), inv_txt, fill=text_color, font=font_small)
			logger.debug(f"Inventory section rendered: {inv_txt}")

		# Accent line
		try:
			accent_y = card_height - 8
			draw.rectangle((0, accent_y, card_width, card_height), fill=theme_palette["accent"])
			logger.debug("Accent line rendered")
		except Exception as e:
			logger.warning(f"Accent line rendering failed: {e}")

		# Wait for avatar and apply it
		try:
			avatar = await avatar_task
			if avatar:
				# Create circular mask
				mask = Image.new("L", (avatar_size, avatar_size), 0)
				mask_draw = ImageDraw.Draw(mask)
				mask_draw.ellipse((0, 0, avatar_size, avatar_size), fill=255)
				card.paste(avatar, avatar_pos, mask)
				logger.debug("Avatar applied successfully")
			else:
				logger.warning("Avatar could not be loaded")
		except Exception as e:
			logger.error(f"Avatar processing failed: {e}")

		logger.info("Profile card generation completed successfully")
		return card


async def _fetch_avatar_optimized(avatar_url: str, size: int) -> Optional[Image.Image]:
	"""Optimized avatar fetching with caching and error handling."""
	with PerformanceLogger(logger, f"fetch_avatar size={size}"):
		logger.debug(f"Fetching avatar from: {avatar_url}")
		try:
			session = _get_session()
			async with session.get(avatar_url) as resp:
				if resp.status == 200:
					data = await resp.read()
					avatar = Image.open(BytesIO(data)).convert("RGBA")
					resized_avatar = avatar.resize((size, size), Image.Resampling.LANCZOS)
					logger.debug(f"Avatar fetched and resized successfully: {len(data)} bytes")
					return resized_avatar
				else:
					logger.warning(f"Avatar fetch failed with status {resp.status}")
		except Exception as e:
			logger.error(f"Avatar fetch failed: {e}")
		return None


async def setup(bot: commands.Bot):
	"""Function used to load the Profile cog."""
	logger.info("Setting up Profile cog")
	await bot.add_cog(Profile(bot))
	logger.info("Profile cog setup completed")