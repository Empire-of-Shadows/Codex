import logging
import re
import time
from typing import Dict, Tuple, Set, Optional

import discord
from discord import app_commands, Interaction
from discord.ext import commands, tasks
from discord.ext.commands import BucketType

from commands.ce_utilities.helpers.colors import tier_1, tier_2, tier_3, tier_4
from commands.ce_utilities.helpers.embed import EditEmbedModal
from commands.ce_utilities.helpers.embed_modal import EmbedModal
from utils.bot import s
from utils.cooldown import create_color_cooldown, create_cooldown, create_features, edit_cooldown

from utils.logger import get_logger, log_context, log_performance

logger = get_logger("CreateEmbed")

# Combine all tiers into a dictionary for easy access
tiers: Dict[str, Dict[str, int]] = {
	"tier_1": tier_1,
	"tier_2": tier_2,
	"tier_3": tier_3,
	"tier_4": tier_4,
}

# Role-to-tier mapping for allowed colors
role_to_tier_mapping: Dict[int, Set[str]] = {
	1364034812452798545: {"tier_1"},  # Silver Fang
	1364034820820177038: {"tier_1", "tier_2"},  # Golden Snake
	1364034825618718793: {"tier_1", "tier_2", "tier_3"},  # Platinum Ghost
	1364034830526054490: {"tier_1", "tier_2", "tier_3", "tier_4"},  # Diamond Wraith
	1364038955120721920: {"tier_1", "tier_2", "tier_3", "tier_4"},  # Mystic Dragon
	1362551055413543023: {"tier_1", "tier_2", "tier_3", "tier_4"},  # Moderator
	1362166614451032346: {"tier_1", "tier_2", "tier_3", "tier_4"},  # Admin
	1365522726206443550: {"tier_1", "tier_2", "tier_3", "tier_4"},  # Special role
}

# Enhanced role-based feature access (tier-based unlocks)
FEATURE_ACCESS: Dict[str, Set[int]] = {
	"basic_embed": {
		1364034812452798545,  # Silver Fang
		1364034820820177038,  # Golden Snake
		1364034825618718793,  # Platinum Ghost
		1364034830526054490,  # Diamond Wraith
		1364038955120721920,  # Mystic Dragon
		1362551055413543023,  # Moderator
		1362166614451032346,  # Admin
		1365522726206443550,  # Special role
	},
	"image_field": {
		1364034820820177038,  # Golden Snake
		1364034825618718793,  # Platinum Ghost
		1364034830526054490,  # Diamond Wraith
		1364038955120721920,  # Mystic Dragon
		1362551055413543023,  # Moderator
		1362166614451032346,  # Admin
		1365522726206443550,  # Special role
	},
	"author_field": {
		1364034825618718793,  # Platinum Ghost
		1364034830526054490,  # Diamond Wraith
		1364038955120721920,  # Mystic Dragon
		1362551055413543023,  # Moderator
		1362166614451032346,  # Admin
		1365522726206443550,  # Special role
	},
	"footer_field": {
		1364034830526054490,  # Diamond Wraith
		1364038955120721920,  # Mystic Dragon
		1362551055413543023,  # Moderator
		1362166614451032346,  # Admin
		1365522726206443550,  # Special role
	},
	"timestamp": {
		1364038955120721920,  # Mystic Dragon
		1362551055413543023,  # Moderator
		1362166614451032346,  # Admin
		1365522726206443550,  # Special role
	}
}

# Authorization cache (message_id -> {"user_id": int, "expires": float})
authorization_cache: Dict[int, Dict[str, int | float]] = {}

# Cache expiration time (seconds)
CACHE_DURATION = 3600
# Soft cap cache entries to prevent unbounded growth
MAX_CACHE_ENTRIES = 2000


async def is_admin_check(interaction: Interaction) -> bool:
	"""
    Return True if the user is an admin or has a specific owner/admin ID.
    """
	is_admin = (
			interaction.user.guild_permissions.administrator
			or interaction.user.id == 1362166614451032346
	)
	logger.debug(f"Admin check for {interaction.user.id}: {is_admin}")
	return is_admin


@log_performance("get_allowed_colors")
def get_allowed_colors(user_roles: Set[int]) -> Dict[str, int]:
	"""
    Retrieve allowed colors for a user based on their roles.
    """
	logger.debug(f"Getting allowed colors for roles: {user_roles}")
	allowed_tiers: Set[str] = set()
	for role_id in user_roles:
		allowed_tiers.update(role_to_tier_mapping.get(role_id, set()))

	# Combine all colors from the accessible tiers
	allowed_colors: Dict[str, int] = {}
	for tier in allowed_tiers:
		allowed_colors.update(tiers[tier])

	logger.debug(f"User has access to {len(allowed_colors)} colors from tiers: {allowed_tiers}")
	return allowed_colors


def get_user_features(user_roles: Set[int]) -> Set[str]:
	"""
    Get available features for a user based on their roles.
    """
	logger.debug(f"Getting user features for roles: {user_roles}")
	available_features = set()
	for feature, required_roles in FEATURE_ACCESS.items():
		if any(role_id in required_roles for role_id in user_roles):
			available_features.add(feature)

	logger.debug(f"User has access to features: {available_features}")
	return available_features


def _parse_message_ref(message_link_or_channel_id: str, message_id: Optional[str]) -> Tuple[int, int]:
	"""
    Robustly parse a Discord message link or a pair of channel_id/message_id strings.
    Supports:
    - https://discord.com/channels/<guild>/<channel>/<message>
    - https://discordapp.com/channels/<guild>/<channel>/<message>
    - https://ptb.discord.com/channels/<guild>/<channel>/<message>
    - https://canary.discord.com/channels/<guild>/<channel>/<message>
    - /channels/@me/<channel>/<message> (DM jump link)
    - "<channel>/<message>" shorthand
    Returns (channel_id, message_id) as integers or raises ValueError.
    """
	logger.debug(
		f"Parsing message reference: link_or_channel='{message_link_or_channel_id}', message_id='{message_id}'")

	if message_id is None:
		link = message_link_or_channel_id.strip()
		# Accept full message link
		pattern = r"(?:https?://)?(?:\w+\.)?discord(?:app)?\.com/channels/(?:@me|\d+)/(\d+)/(\d+)$"
		m = re.search(pattern, link)
		if m:
			channel_id, parsed_message_id = int(m.group(1)), int(m.group(2))
			logger.debug(f"Parsed from URL: channel={channel_id}, message={parsed_message_id}")
			return channel_id, parsed_message_id
		# Fallback: try simple "channel_id/message_id"
		parts = link.split("/")
		if len(parts) >= 2 and parts[-2].isdigit() and parts[-1].isdigit():
			channel_id, parsed_message_id = int(parts[-2]), int(parts[-1])
			logger.debug(f"Parsed from shorthand: channel={channel_id}, message={parsed_message_id}")
			return channel_id, parsed_message_id
		logger.warning(f"Failed to parse message link: '{link}'")
		raise ValueError("Invalid message link format.")
	# Channel and message provided separately
	if not message_link_or_channel_id.isdigit() or not message_id.isdigit():
		logger.warning(f"Non-numeric IDs provided: channel='{message_link_or_channel_id}', message='{message_id}'")
		raise ValueError("Channel ID and Message ID must be numeric.")

	channel_id, parsed_message_id = int(message_link_or_channel_id), int(message_id)
	logger.debug(f"Parsed from separate IDs: channel={channel_id}, message={parsed_message_id}")
	return channel_id, parsed_message_id


def _build_colors_embed(allowed_colors: Dict[str, int]) -> discord.Embed:
	"""
    Build an embed listing allowed colors grouped by tier names.
    """
	logger.debug(f"Building colors embed for {len(allowed_colors)} colors")

	# Organize colors by tiers
	tiered_colors: Dict[str, list[str]] = {tier: [] for tier in tiers.keys()}
	for name, code in allowed_colors.items():
		for tier, tier_colors in tiers.items():
			if name in tier_colors:
				tiered_colors[tier].append(f"`{name}`: #{code:06X}")

	embed = discord.Embed(
		title="Available Colors",
		description="Here are the embed colors you can use based on your roles.",
		color=discord.Color.blurple(),
	)
	for tier_name, color_list in tiered_colors.items():
		if color_list:
			embed.add_field(name=f"{tier_name.replace('_', ' ').title()}", value="\n".join(color_list), inline=False)
	embed.set_footer(text="Note: Color usage is restricted by your roles.")

	logger.debug("Colors embed successfully built")
	return embed


def _build_features_embed(user_features: Set[str]) -> discord.Embed:
	"""
    Build an embed showing available features for the user.
    """
	logger.debug(f"Building features embed for {len(user_features)} features")

	embed = discord.Embed(
		title="Available Features",
		description="Here are the embed features you can access based on your roles:",
		color=discord.Color.green(),
	)

	feature_descriptions = {
		"basic_embed": "✅ Create basic embeds with title, description, and color",
		"image_field": "🖼️ Add images and thumbnails to embeds",
		"author_field": "👤 Add author field with name and icon",
		"footer_field": "📝 Add footer text to embeds",
		"timestamp": "⏰ Add timestamps to embeds"
	}

	features_text = []
	for feature in sorted(user_features):
		if feature in feature_descriptions:
			features_text.append(feature_descriptions[feature])

	if features_text:
		embed.description += "\n\n" + "\n".join(features_text)
	else:
		embed.description = "❌ No features available for your role."

	embed.set_footer(text="Upgrade your role to unlock more features!")

	logger.debug("Features embed successfully built")
	return embed


class EmbedGroup(commands.GroupCog, name="embed", description="Create and edit embeds with role-based limits"):
	"""
    Group cog providing:
    - /embed create
    - /embed edit
    - /embed colors (lists allowed colors)
    - /embed features (lists available features)
    """

	def __init__(self, bot: commands.Bot):
		self.bot = bot
		logger.info("Initializing EmbedGroup cog")
		self.cleanup_cache.start()  # Periodically clean expired cache entries
		logger.info("EmbedGroup cog initialized successfully")

	def cog_unload(self) -> None:
		"""
        Ensure background tasks are properly cancelled on cog unload.
        """
		logger.info("Unloading EmbedGroup cog")
		self.cleanup_cache.cancel()
		logger.info("EmbedGroup cog unloaded successfully")

	@tasks.loop(minutes=10)
	async def cleanup_cache(self):
		"""Periodically remove expired entries from the authorization cache."""
		with log_context(logger, "cache_cleanup"):
			now = time.time()
			expired_keys = [key for key, data in authorization_cache.items() if data["expires"] <= now]

			for key in expired_keys:
				del authorization_cache[key]

			if expired_keys:
				logger.info(f"Cleaned up {len(expired_keys)} expired entries in the authorization cache")

			logger.debug(f"Cache cleanup completed. Current cache size: {len(authorization_cache)}")

	@cleanup_cache.before_loop
	async def _before_cleanup_cache(self):
		# Wait for bot to be fully ready before starting the loop
		logger.debug("Waiting for bot to be ready before starting cache cleanup task")
		await self.bot.wait_until_ready()
		logger.debug("Bot is ready, cache cleanup task can begin")

	async def update_cache(self, user_id: int, message_id: int):
		"""
        Add message ownership to the cache and enforce a soft cap to avoid unbounded growth.
        """
		logger.debug(f"Updating cache for user {user_id}, message {message_id}")

		# Soft-evict earliest-expiring entries if above the cap
		if len(authorization_cache) >= MAX_CACHE_ENTRIES:
			logger.warning(
				f"Cache at capacity ({len(authorization_cache)}/{MAX_CACHE_ENTRIES}), evicting oldest entries")
			# Remove up to 50 oldest entries at once
			oldest = sorted(authorization_cache.items(), key=lambda kv: kv[1]["expires"])[:50]
			for mid, _ in oldest:
				authorization_cache.pop(mid, None)
			logger.info(f"Evicted {len(oldest)} old cache entries")

		authorization_cache[message_id] = {
			"user_id": user_id,
			"expires": time.time() + CACHE_DURATION,
		}
		logger.debug(f"Cache updated for message {message_id} by user {user_id}")

	# /embed create
	@app_commands.command(
		name="create",
		description="Create an embed via modal. Role-based description length and color access."
	)
	@app_commands.checks.has_any_role(
		1364034812452798545,  # Silver Fang
		1364034820820177038,  # Golden Snake
		1364034825618718793,  # Platinum Ghost
		1364034830526054490,  # Diamond Wraith
		1364038955120721920,  # Mystic Dragon
		1362551055413543023,  # Moderator
		1362166614451032346  # Administrator
	)
	@app_commands.check(create_cooldown())
	async def create(self, interaction: discord.Interaction):
		"""
        Open a modal to create an embed. On success, the created message is cached
        to allow the author to edit it for a limited time.
        """
		with log_context(logger, f"embed_create_command", logging.INFO):
			logger.info(
				f"Command /embed create invoked by {interaction.user} (ID: {interaction.user.id}) in guild {interaction.guild_id}")
			user_roles = {role.id for role in interaction.user.roles}
			logger.debug(f"User roles: {user_roles}")

			try:
				await self._open_create_modal(interaction, user_roles)
				logger.info(f"Create modal successfully presented to {interaction.user}")
			except Exception as e:
				logger.error(f"Error presenting create modal to {interaction.user}: {e}", exc_info=True)
				if not interaction.response.is_done():
					await interaction.response.send_message(
						"❌ An internal error occurred while opening the modal.", ephemeral=True
					)

	# /embed colors
	@app_commands.command(
		name="colors",
		description="List the embed colors you are allowed to use."
	)
	@app_commands.checks.has_any_role(
		1364034812452798545,  # Silver Fang
		1364034820820177038,  # Golden Snake
		1364034825618718793,  # Platinum Ghost
		1364034830526054490,  # Diamond Wraith
		1364038955120721920,  # Mystic Dragon
		1362551055413543023,  # Moderator
		1362166614451032346  # Administrator
	)
	@app_commands.check(create_color_cooldown())
	async def colors(self, interaction: discord.Interaction):
		"""
        Display a list of accessible colors based on user roles.
        """
		with log_context(logger, f"embed_colors_command", logging.INFO):
			logger.info(f"Command /embed colors invoked by {interaction.user} (ID: {interaction.user.id})")
			user_roles = {role.id for role in interaction.user.roles}

			allowed_colors = get_allowed_colors(user_roles)
			if not allowed_colors:
				logger.info(f"User {interaction.user} has no color access")
				await interaction.response.send_message("❌ You do not have access to any colors.", ephemeral=True)
				return

			embed = _build_colors_embed(allowed_colors)
			await interaction.response.send_message(embed=embed, ephemeral=True)
			logger.info(f"Available colors sent to user {interaction.user} ({len(allowed_colors)} colors)")

	# /embed features
	@app_commands.command(
		name="features",
		description="List the embed features you can access based on your roles."
	)
	@app_commands.checks.has_any_role(
		1364034812452798545,  # Silver Fang
		1364034820820177038,  # Golden Snake
		1364034825618718793,  # Platinum Ghost
		1364034830526054490,  # Diamond Wraith
		1364038955120721920,  # Mystic Dragon
		1362551055413543023,  # Moderator
		1362166614451032346  # Administrator
	)
	@app_commands.check(create_features())
	async def features(self, interaction: discord.Interaction):
		"""
        Display available features based on user roles.
        """
		with log_context(logger, f"embed_features_command", logging.INFO):
			logger.info(f"Command /embed features invoked by {interaction.user} (ID: {interaction.user.id})")
			user_roles = {role.id for role in interaction.user.roles}

			user_features = get_user_features(user_roles)
			embed = _build_features_embed(user_features)
			await interaction.response.send_message(embed=embed, ephemeral=True)
			logger.info(f"Available features sent to user {interaction.user} ({len(user_features)} features)")

	# /embed edit
	@app_commands.command(
		name="edit",
		description="Edit an existing bot embed by link or by channel/message IDs."
	)
	@app_commands.describe(
		message_link_or_channel_id="A full message link, or a channel ID if message_id is provided",
		message_id="Message ID (optional if a full message link is provided)"
	)
	@app_commands.check(edit_cooldown())
	async def edit(
			self,
			interaction: discord.Interaction,
			message_link_or_channel_id: str,
			message_id: Optional[str] = None,
	):
		"""
        Edit a bot-authored embed if authorized. Non-admins must be the creator of the embed
        and within the authorization window.
        """
		with log_context(logger, f"embed_edit_command", logging.INFO):
			logger.info(f"Command /embed edit invoked by {interaction.user} (ID: {interaction.user.id})")

			# Parse and validate the input
			try:
				channel_id, parsed_message_id = _parse_message_ref(message_link_or_channel_id, message_id)
				logger.debug(f"Parsed message reference: channel={channel_id}, message={parsed_message_id}")
			except ValueError as e:
				logger.warning(f"Invalid message format provided by {interaction.user}: {e}")
				await interaction.response.send_message("❌ Invalid message link or ID format.", ephemeral=True)
				return

			# Check if the user is an admin and skip cache validation if they are
			if await is_admin_check(interaction):
				logger.info(f"Admin privileges detected for user {interaction.user}")
			else:
				now = time.time()
				if parsed_message_id not in authorization_cache:
					logger.warning(
						f"Unauthorized edit attempt for message {parsed_message_id} by {interaction.user} - not in cache")
					await interaction.response.send_message(
						"❌ You cannot edit this embed because the session has expired. Please recreate it using `/embed create`.",
						ephemeral=True,
					)
					return

				data = authorization_cache[parsed_message_id]
				if data["user_id"] != interaction.user.id:
					logger.warning(
						f"Unauthorized edit attempt for message {parsed_message_id} by {interaction.user} - wrong user (cache has {data['user_id']})")
					await interaction.response.send_message("❌ You are not authorized to edit this embed.",
															ephemeral=True)
					return
				if data["expires"] <= now:
					logger.info(f"Authorization expired for message {parsed_message_id} by {interaction.user}")
					del authorization_cache[parsed_message_id]
					await interaction.response.send_message("❌ Your authorization session has expired.", ephemeral=True)
					return

			# Fetch and validate the message
			try:
				channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
				message = await channel.fetch_message(parsed_message_id)
				logger.debug(f"Successfully fetched message {parsed_message_id} from channel {channel_id}")
			except discord.NotFound:
				logger.warning(f"Message {parsed_message_id} or channel {channel_id} not found")
				await interaction.response.send_message("❌ Message or channel not found.", ephemeral=True)
				return
			except discord.Forbidden:
				logger.warning(f"Access forbidden to channel {channel_id} / message {parsed_message_id}")
				await interaction.response.send_message("❌ Access to this channel or message is forbidden.",
														ephemeral=True)
				return
			except Exception as e:
				logger.error(f"Fetch error for channel {channel_id} / message {parsed_message_id}: {e}", exc_info=True)
				await interaction.response.send_message("❌ Failed to fetch the message.", ephemeral=True)
				return

			if not message.author.bot or not message.embeds:
				logger.warning(
					f"Invalid embed edit target - not a bot embed. Author: {message.author}, Embeds: {len(message.embeds)}")
				await interaction.response.send_message("❌ This is not a valid bot embed.", ephemeral=True)
				return

			# Retrieve user roles as IDs
			user_roles = {role.id for role in interaction.user.roles}

			# Send modal for editing
			try:
				await interaction.response.send_modal(EditEmbedModal(message, user_roles))
				logger.info(f"Edit modal sent for message {parsed_message_id} to {interaction.user}")
			except Exception as e:
				logger.error(f"Failed to send edit modal: {e}", exc_info=True)
				if not interaction.response.is_done():
					await interaction.response.send_message("❌ Failed to process the edit modal.", ephemeral=True)

	# Internal helper to open the create modal with cache callback
	async def _open_create_modal(self, interaction: discord.Interaction, user_roles: Set[int]):
		"""
        Present the embed creation modal and attach a callback to update the authorization cache.
        """
		try:
			cache_update_callback = self.update_cache
			modal = EmbedModal(user_roles=user_roles, cache_update_callback=cache_update_callback)
			await interaction.response.send_modal(modal)
			logger.info(f"Embed creation modal sent to {interaction.user}")
		except Exception as e:
			logger.error(f"Error opening create modal for {interaction.user}: {e}", exc_info=True)
			if not interaction.response.is_done():
				await interaction.response.send_message(
					"❌ An internal error occurred. Please contact the admin.", ephemeral=True
				)

	# Unified error handlers for subcommands
	@create.error
	async def _create_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
		try:
			if isinstance(error, app_commands.CheckFailure):
				logger.info(f"Create command access denied for {interaction.user}: {type(error).__name__}")
				await interaction.response.send_message("❌ You don't have permission to use this command.",
														ephemeral=True)
			else:
				logger.error(f"Unhandled error in /embed create for {interaction.user}: {error}", exc_info=True)
				if not interaction.response.is_done():
					await interaction.response.send_message("❌ Something went wrong. Please try again later.",
															ephemeral=True)
		except discord.errors.NotFound:
			# Interaction expired, log and ignore
			logger.warning(f"Interaction expired while handling create error for {interaction.user}")
		except Exception as inner_e:
			logger.error(f"Error in create error handler: {inner_e}", exc_info=True)

	@edit.error
	async def _edit_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
		try:
			if isinstance(error, app_commands.CheckFailure):
				logger.info(f"Edit command access denied for {interaction.user}: {type(error).__name__}")
				await interaction.response.send_message("❌ You're on cooldown or lack permission to use this command.",
														ephemeral=True)
			else:
				logger.error(f"Unhandled error in /embed edit for {interaction.user}: {error}", exc_info=True)
				if not interaction.response.is_done():
					await interaction.response.send_message("❌ Something went wrong. Please try again later.",
															ephemeral=True)
		except discord.errors.NotFound:
			# Interaction expired, log and ignore
			logger.warning(f"Interaction expired while handling edit error for {interaction.user}")
		except Exception as inner_e:
			logger.error(f"Error in edit error handler: {inner_e}", exc_info=True)

	@colors.error
	async def _colors_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
		try:
			if isinstance(error, app_commands.CheckFailure):
				logger.info(f"Colors command access denied for {interaction.user}: {type(error).__name__}")
				await interaction.response.send_message("❌ You don't have permission to view colors.", ephemeral=True)
			else:
				logger.error(f"Unhandled error in /embed colors for {interaction.user}: {error}", exc_info=True)
				if not interaction.response.is_done():
					await interaction.response.send_message("❌ Something went wrong. Please try again later.",
															ephemeral=True)
		except discord.errors.NotFound:
			# Interaction expired, log and ignore
			logger.warning(f"Interaction expired while handling colors error for {interaction.user}")
		except Exception as inner_e:
			logger.error(f"Error in colors error handler: {inner_e}", exc_info=True)

	@features.error
	async def _features_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
		try:
			if isinstance(error, app_commands.CheckFailure):
				logger.info(f"Features command access denied for {interaction.user}: {type(error).__name__}")
				await interaction.response.send_message("❌ You don't have permission to view features.", ephemeral=True)
			else:
				logger.error(f"Unhandled error in /embed features for {interaction.user}: {error}", exc_info=True)
				if not interaction.response.is_done():
					await interaction.response.send_message("❌ Something went wrong. Please try again later.",
															ephemeral=True)
		except discord.errors.NotFound:
			# Interaction expired, log and ignore
			logger.warning(f"Interaction expired while handling features error for {interaction.user}")
		except Exception as inner_e:
			logger.error(f"Error in features error handler: {inner_e}", exc_info=True)


async def setup(bot: commands.Bot):
	logger.info("Setting up EmbedGroup cog")
	await bot.add_cog(EmbedGroup(bot))
	logger.info("EmbedGroup cog setup completed")