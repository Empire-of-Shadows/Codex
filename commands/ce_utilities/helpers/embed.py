# Python
import logging
import re
from typing import Optional, Set

import discord
from commands.ce_utilities.helpers.embed_modal import get_allowed_colors, get_max_description_length
from utils.logger import get_logger, log_context

logger = get_logger("EmbedEditModal")


def _is_valid_image_url(url: str) -> bool:
	"""
    Basic validation for thumbnail URLs. Accepts http(s) URLs with common image extensions.
    """
	logger.debug(f"Validating image URL: {url[:100]}{'...' if len(url) > 100 else ''}")

	if not url:
		logger.debug("Image URL validation failed: empty URL")
		return False
	if not url.startswith(("http://", "https://")):
		logger.debug(f"Image URL validation failed: invalid protocol - {url[:50]}")
		return False
	# Allow common image extensions as a soft check. Discord can still load other valid images.
	is_valid = bool(re.search(r"\.(png|jpe?g|gif|webp|bmp|svg)(?:\?.*)?$", url, flags=re.IGNORECASE))

	if is_valid:
		logger.debug(f"Image URL validation passed: {url[:100]}{'...' if len(url) > 100 else ''}")
	else:
		logger.debug(f"Image URL validation failed: no valid extension found - {url[:50]}")

	return is_valid


def _parse_color(color_str: str) -> Optional[int]:
	"""
    Parse a color string into an integer RGB value.
    Accepts:
    - Hex with or without '#', case-insensitive
    - 0x-prefixed hex
    Returns int or None if invalid.
    """
	logger.debug(f"Parsing color string: '{color_str}'")

	s = color_str.strip().lower()
	if not s:
		logger.debug("Color parsing failed: empty string")
		return None

	original_s = s
	if s.startswith("#"):
		s = s[1:]
		logger.debug(f"Removed # prefix: '{s}'")
	elif s.startswith("0x"):
		s = s[2:]
		logger.debug(f"Removed 0x prefix: '{s}'")

	# Must be 3, 6, or 8 hex digits (we'll treat 3 as shorthand RGB)
	if not re.fullmatch(r"[0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8}", s):
		logger.debug(f"Color parsing failed: invalid hex format - '{s}' (original: '{original_s}')")
		return None

	# Expand 3-digit shorthand to 6-digit
	if len(s) == 3:
		s = "".join(ch * 2 for ch in s)
		logger.debug(f"Expanded 3-digit hex to 6-digit: '{s}'")

	# If 8 digits (ARGB), drop alpha to use RGB
	if len(s) == 8:
		s = s[2:]
		logger.debug(f"Dropped alpha channel from 8-digit hex: '{s}'")

	try:
		color_value = int(s, 16)
		logger.debug(f"Successfully parsed color '{original_s}' to {color_value} (0x{color_value:06x})")
		return color_value
	except ValueError as e:
		logger.debug(f"Color parsing failed: ValueError for '{s}' (original: '{original_s}'): {e}")
		return None


class EditEmbedModal(discord.ui.Modal, title="Edit Embed"):
	def __init__(self, message: discord.Message, user_roles: Set[int]):
		super().__init__()
		self.message = message
		self.user_roles = user_roles
		embed = message.embeds[0] if message.embeds else discord.Embed()

		logger.info(f"Initializing embed edit modal for message {message.id} by user with roles: {user_roles}")

		# Centralized role-based maximum description length (non-stacking, capped to 4000)
		self.max_length = get_max_description_length(self.user_roles)
		logger.debug(f"Maximum description length for user roles {user_roles}: {self.max_length}")

		# Log current embed state
		logger.debug(f"Current embed state - Title: {'SET' if embed.title else 'EMPTY'}, "
					 f"Description: {len(embed.description) if embed.description else 0} chars, "
					 f"Thumbnail: {'SET' if getattr(embed, 'thumbnail', None) else 'EMPTY'}, "
					 f"Color: {f'#{embed.color.value:06x}' if embed.color else 'EMPTY'}")

		self.title_input = discord.ui.TextInput(
			label="New Embed Title",
			placeholder="Leave empty to keep current title",
			default=embed.title or "",
			required=False,
			max_length=256,
		)
		self.description_input = discord.ui.TextInput(
			label="New Embed Description",
			placeholder=f"Leave empty to keep current description (Max {self.max_length} characters)",
			style=discord.TextStyle.paragraph,
			default=embed.description or "",
			required=False,
			max_length=self.max_length,
		)
		self.thumbnail_input = discord.ui.TextInput(
			label="New Embed Thumbnail URL",
			placeholder="Leave empty to keep current thumbnail or type 'none' to remove",
			default=(embed.thumbnail.url if getattr(embed, "thumbnail", None) else ""),
			required=False,
		)
		self.color_input = discord.ui.TextInput(
			label="New color",
			placeholder="Choose a new color by hex (#RRGGBB) or allowed name",
			default=(f"#{embed.color.value:06x}" if embed.color else ""),
			required=False,
			max_length=32,
		)

		self.add_item(self.title_input)
		self.add_item(self.description_input)
		self.add_item(self.thumbnail_input)
		self.add_item(self.color_input)

		logger.debug("Embed edit modal initialized successfully")

	async def on_submit(self, interaction: discord.Interaction):
		with log_context(logger, f"embed edit submission for message {self.message.id}", logging.INFO):
			logger.info(f"Embed edit submitted by user {interaction.user.id} ({interaction.user.display_name}) "
						f"for message {self.message.id} in channel {interaction.channel_id}")

			# Log submitted values (sanitized)
			logger.debug(f"Submitted values - Title: {'SET' if self.title_input.value else 'EMPTY'}, "
						 f"Description: {len(self.description_input.value) if self.description_input.value else 0} chars, "
						 f"Thumbnail: {'SET' if self.thumbnail_input.value else 'EMPTY'}, "
						 f"Color: '{self.color_input.value}'" if self.color_input.value else "EMPTY")

			original_embed = self.message.embeds[0] if self.message.embeds else discord.Embed()
			embed = original_embed.copy()

			changed = False
			changes_made = []

			# Update Title
			if self.title_input.value and self.title_input.value != (original_embed.title or ""):
				logger.debug(
					f"Title change detected: '{original_embed.title or '(empty)'}' -> '{self.title_input.value}'")
				embed.title = self.title_input.value
				changed = True
				changes_made.append("title")

			# Update Description
			if self.description_input.value and self.description_input.value != (original_embed.description or ""):
				if len(self.description_input.value) > self.max_length:
					logger.warning(
						f"Description length exceeded: {len(self.description_input.value)} > {self.max_length} "
						f"for user {interaction.user.id} with roles {self.user_roles}")
					await interaction.response.send_message(
						f"❌ Description exceeds maximum length of {self.max_length} characters.",
						ephemeral=True,
					)
					return

				logger.debug(f"Description change detected: {len(original_embed.description or '')} chars -> "
							 f"{len(self.description_input.value)} chars")
				embed.description = self.description_input.value
				changed = True
				changes_made.append("description")

			# Handle Thumbnail
			thumb_val = (self.thumbnail_input.value or "").strip()
			if thumb_val:
				if thumb_val.lower() == "none":
					logger.debug("Thumbnail removal requested")
					embed.set_thumbnail(url=None)
					changed = True
					changes_made.append("thumbnail_removed")
				elif _is_valid_image_url(thumb_val):
					logger.debug(f"Valid thumbnail URL provided, updating thumbnail")
					embed.set_thumbnail(url=thumb_val)
					changed = True
					changes_made.append("thumbnail_set")
				else:
					logger.warning(f"Invalid thumbnail URL provided by user {interaction.user.id}: {thumb_val[:100]}")
					await interaction.response.send_message(
						"❌ Invalid thumbnail URL. Please provide a valid http(s) image URL or 'none' to remove.",
						ephemeral=True,
					)
					return

			# Handle Color
			allowed_colors = get_allowed_colors(self.user_roles)
			logger.debug(
				f"User has {len(allowed_colors)} allowed colors: {list(allowed_colors.keys()) if allowed_colors else 'unlimited'}")

			if self.color_input.value:
				color_str = self.color_input.value.strip()
				color_key = color_str.lower()

				# Try named color first
				if color_key in allowed_colors:
					new_color_val = allowed_colors[color_key]
					logger.debug(f"Using named color '{color_key}' with value {new_color_val} (0x{new_color_val:06x})")
				else:
					# Try hex parsing
					parsed = _parse_color(color_str)
					if parsed is None:
						logger.warning(f"Invalid color format provided by user {interaction.user.id}: '{color_str}'")
						await interaction.response.send_message(
							f"❌ Invalid color '{color_str}'. Use a valid hex color or an allowed name.",
							ephemeral=True,
						)
						return
					new_color_val = parsed

					# Enforce authorization (hex must be within allowed set)
					if allowed_colors and new_color_val not in allowed_colors.values():
						logger.warning(
							f"Unauthorized color {new_color_val} (0x{new_color_val:06x}) attempted by user {interaction.user.id} "
							f"with roles {self.user_roles}. Allowed colors: {list(allowed_colors.values())}")
						await interaction.response.send_message(
							f"❌ Color '{color_str}' is not authorized for your roles.",
							ephemeral=True,
						)
						return

				if not embed.color or embed.color.value != new_color_val:
					old_color = f"0x{embed.color.value:06x}" if embed.color else "none"
					logger.debug(f"Color change detected: {old_color} -> 0x{new_color_val:06x}")
					embed.color = discord.Color(new_color_val)
					changed = True
					changes_made.append("color")

			if not changed:
				logger.info(f"No changes detected for embed edit by user {interaction.user.id}")
				await interaction.response.send_message(
					"ℹ️ Nothing to update. No changes were detected.",
					ephemeral=True,
				)
				return

			logger.info(f"Applying embed changes: {', '.join(changes_made)} for message {self.message.id}")

			# Apply Edits
			try:
				await self.message.edit(embed=embed)
				logger.info(
					f"Embed successfully updated by user {interaction.user.id} ({interaction.user.display_name}) "
					f"for message {self.message.id}. Changes: {', '.join(changes_made)}")
				await interaction.response.send_message("✅ Embed updated successfully.", ephemeral=True)
			except discord.HTTPException as e:
				logger.error(
					f"Discord HTTP error editing embed (message_id={self.message.id}, user_id={interaction.user.id}): "
					f"{e.status} {e.text}", exc_info=True)
				error_msg = "❌ Failed to edit the embed. Please try again later."

				# If response already sent, try followup; else send response
				if interaction.response.is_done():
					await interaction.followup.send(error_msg, ephemeral=True)
				else:
					await interaction.response.send_message(error_msg, ephemeral=True)
			except discord.Forbidden as e:
				logger.error(
					f"Forbidden error editing embed (message_id={self.message.id}, user_id={interaction.user.id}): "
					f"Bot lacks permissions", exc_info=True)
				error_msg = "❌ I don't have permission to edit this message."

				if interaction.response.is_done():
					await interaction.followup.send(error_msg, ephemeral=True)
				else:
					await interaction.response.send_message(error_msg, ephemeral=True)
			except Exception as e:
				logger.error(
					f"Unexpected error editing embed (message_id={self.message.id}, user_id={interaction.user.id}): {e}",
					exc_info=True)
				error_msg = "❌ An unexpected error occurred. Please try again later."

				if interaction.response.is_done():
					await interaction.followup.send(error_msg, ephemeral=True)
				else:
					await interaction.response.send_message(error_msg, ephemeral=True)