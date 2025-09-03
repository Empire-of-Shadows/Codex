import logging
import time
from typing import Optional, Set
from enum import IntEnum

from discord import Interaction
from discord.ext import commands
from discord.ext.commands import CooldownMapping, BucketType, CommandOnCooldown

from utils.logger import get_logger, log_context

logger = get_logger("Cooldown")


class AdminRoles(IntEnum):
	"""Admin role IDs that bypass cooldowns."""
	ADMIN = 1362166614451032346


class CooldownConfig:
	"""Centralized cooldown configuration."""
	CREATE = CooldownMapping.from_cooldown(1, 300, BucketType.user)
	CREATE_COLOR = CooldownMapping.from_cooldown(4, 10, BucketType.user)
	CREATE_FEATURES = CooldownMapping.from_cooldown(1, 10, BucketType.user)
	EDIT = CooldownMapping.from_cooldown(1, 60, BucketType.user)
	CLONE = CooldownMapping.from_cooldown(1, 10, BucketType.user)
	WELCOME = CooldownMapping.from_cooldown(1, 10, BucketType.user)


def format_time(seconds: float) -> str:
	"""Format time in a more human-readable way."""
	if seconds < 60:
		return f"{seconds:.1f} seconds"
	elif seconds < 3600:
		minutes = int(seconds // 60)
		remaining_seconds = int(seconds % 60)
		return f"{minutes}m {remaining_seconds}s"
	else:
		hours = int(seconds // 3600)
		minutes = int((seconds % 3600) // 60)
		return f"{hours}h {minutes}m"


def has_admin_role(user_roles: Set[int]) -> bool:
	"""Check if user has any admin roles that bypass cooldowns."""
	is_admin = AdminRoles.ADMIN in user_roles

	if is_admin:
		logger.debug(f"Admin role detected in user roles: {user_roles}")
	else:
		logger.debug(f"No admin roles found in user roles: {user_roles}")

	return is_admin


class FakeContext:
	"""A lightweight context object for cooldown bucket creation."""

	def __init__(self, interaction: Interaction):
		self.author = interaction.user
		self.guild = interaction.guild
		self.channel = interaction.channel


def cooldown_enforcer(cooldown_map: CooldownMapping, bucket_type: BucketType):
	"""
    Creates a cooldown enforcer decorator for Discord slash commands.

    Args:
        cooldown_map: The cooldown mapping to use
        bucket_type: The bucket type for the cooldown

    Returns:
        A decorator function that enforces the cooldown
    """
	# Extract cooldown config for logging
	cooldown_rate = cooldown_map._cooldown.rate
	cooldown_per = cooldown_map._cooldown.per

	logger.info(f"Creating cooldown enforcer - Rate: {cooldown_rate}, Per: {cooldown_per}s, Type: {bucket_type.name}")

	def decorator():
		async def check(interaction: Interaction) -> bool:
			user_id = interaction.user.id
			username = f"{interaction.user.name}#{interaction.user.discriminator}"
			guild_name = interaction.guild.name if interaction.guild else "DM"

			with log_context(logger, f"cooldown_check_{user_id}", level=logging.DEBUG):
				logger.debug(f"Checking cooldown for user: {username} ({user_id}) in guild: {guild_name}")

				# Skip cooldown for admins
				user_role_ids = {role.id for role in interaction.user.roles} if interaction.user.roles else set()

				if has_admin_role(user_role_ids):
					logger.info(f"Admin bypass granted - User: {username} ({user_id}) in {guild_name}")
					return True

				# Create context for bucket retrieval
				fake_ctx = FakeContext(interaction)

				# Get bucket and check cooldown
				bucket = cooldown_map.get_bucket(fake_ctx)
				current_time = time.time()
				retry_after = bucket.update_rate_limit(current_time)

				if retry_after:
					formatted_time = format_time(retry_after)

					logger.warning(f"Cooldown triggered - User: {username} ({user_id}) must wait {formatted_time} | "
								   f"Rate: {cooldown_rate}/{cooldown_per}s | Guild: {guild_name}")

					await interaction.response.send_message(
						f"⏰ You are on cooldown. Try again in **{formatted_time}**.",
						ephemeral=True
					)

					# Log the cooldown exception details
					buffer_time = retry_after * 1.05
					logger.debug(f"Raising CommandOnCooldown exception with buffer: {buffer_time:.2f}s")

					# Add small buffer to retry_after to account for timing discrepancies
					raise CommandOnCooldown(bucket, buffer_time, bucket_type)

				logger.info(f"Cooldown check passed - User: {username} ({user_id}) | "
							f"Rate: {cooldown_rate}/{cooldown_per}s | Guild: {guild_name}")
				return True

		return check

	return decorator


# Convenient pre-configured cooldown decorators with enhanced logging
def _create_named_cooldown(config_attr: str, config: CooldownMapping, bucket_type: BucketType):
	"""Helper to create named cooldown decorators with logging."""
	logger.info(f"Initializing {config_attr} cooldown decorator - "
				f"Rate: {config._cooldown.rate}, Per: {config._cooldown.per}s, Type: {bucket_type.name}")
	return cooldown_enforcer(config, bucket_type)


create_cooldown = _create_named_cooldown("CREATE", CooldownConfig.CREATE, BucketType.user)
create_color_cooldown = _create_named_cooldown("CREATE_COLOR", CooldownConfig.CREATE_COLOR, BucketType.user)
create_features = _create_named_cooldown("CREATE_FEATURES", CooldownConfig.CREATE_FEATURES, BucketType.user)
edit_cooldown = _create_named_cooldown("EDIT", CooldownConfig.EDIT, BucketType.user)
clone_cooldown = _create_named_cooldown("CLONE", CooldownConfig.CLONE, BucketType.user)
welcome_cooldown = _create_named_cooldown("WELCOME", CooldownConfig.WELCOME, BucketType.user)

# Log cooldown system initialization
logger.info("Cooldown system initialized successfully with the following configurations:")
for attr_name in dir(CooldownConfig):
	if not attr_name.startswith('_'):
		config = getattr(CooldownConfig, attr_name)
		logger.info(f"  {attr_name}: {config._cooldown.rate} uses per {config._cooldown.per} seconds")

logger.info(f"Admin roles configured for bypass: {[role.name for role in AdminRoles]}")