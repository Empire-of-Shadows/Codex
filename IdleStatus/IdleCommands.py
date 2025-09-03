import discord
from discord.ext import commands

from IdleStatus.idle import status_manager
from utils.bot import bot

from utils.logger import get_logger

logger = get_logger("IdleCommands")


@bot.command(name="status_add")
@commands.has_permissions(administrator=True)
async def add_status_command(ctx, status_type: str, *, phrase: str):
	"""Add a new status phrase"""
	user_id = ctx.author.id
	guild_id = ctx.guild.id if ctx.guild else None

	logger.info(f"User {user_id} attempting to add {status_type} status: '{phrase}' in guild {guild_id}")

	try:
		if status_manager.add_status(status_type.lower(), phrase):
			logger.info(f"Successfully added {status_type} status: '{phrase}' by user {user_id}")
			await ctx.send(f"✅ Added new {status_type} status: `{phrase}`")
		else:
			logger.warning(f"Failed to add {status_type} status: '{phrase}' by user {user_id} - Invalid status type")
			await ctx.send(f"❌ Invalid status type. Use: playing, watching, listening, streaming")
	except Exception as e:
		logger.error(f"Error adding status by user {user_id}: {e}", exc_info=True)
		await ctx.send("❌ An error occurred while adding the status")


@bot.command(name="status_remove")
@commands.has_permissions(administrator=True)
async def remove_status_command(ctx, status_type: str, *, phrase: str):
	"""Remove a status phrase"""
	user_id = ctx.author.id
	guild_id = ctx.guild.id if ctx.guild else None

	logger.info(f"User {user_id} attempting to remove {status_type} status: '{phrase}' in guild {guild_id}")

	try:
		if status_manager.remove_status(status_type.lower(), phrase):
			logger.info(f"Successfully removed {status_type} status: '{phrase}' by user {user_id}")
			await ctx.send(f"✅ Removed {status_type} status: `{phrase}`")
		else:
			logger.warning(f"Failed to remove {status_type} status: '{phrase}' by user {user_id} - Status not found")
			await ctx.send(f"❌ Status phrase not found")
	except Exception as e:
		logger.error(f"Error removing status by user {user_id}: {e}", exc_info=True)
		await ctx.send("❌ An error occurred while removing the status")


@bot.command(name="status_stats")
async def status_stats_command(ctx):
	"""Show status statistics"""
	user_id = ctx.author.id
	guild_id = ctx.guild.id if ctx.guild else None

	logger.info(f"User {user_id} requested status statistics in guild {guild_id}")

	try:
		stats = status_manager.get_status_stats()
		if not stats:
			logger.info(f"No status history available for stats request by user {user_id}")
			await ctx.send("No status history available")
			return

		embed = discord.Embed(title="Status Statistics", color=0x00ff00)
		embed.add_field(name="Total Changes", value=stats["total_changes"], inline=True)
		embed.add_field(name="Most Used", value=stats["most_used_type"], inline=True)

		distribution = "\n".join([f"{k}: {v}" for k, v in stats["type_distribution"].items()])
		embed.add_field(name="Type Distribution", value=distribution, inline=False)

		logger.info(f"Successfully sent status statistics to user {user_id} - Total changes: {stats['total_changes']}")
		await ctx.send(embed=embed)
	except Exception as e:
		logger.error(f"Error getting status statistics for user {user_id}: {e}", exc_info=True)
		await ctx.send("❌ An error occurred while retrieving status statistics")


@bot.command(name="status_interval")
@commands.has_permissions(administrator=True)
async def set_interval_command(ctx, seconds: int):
	"""Set status rotation interval"""
	user_id = ctx.author.id
	guild_id = ctx.guild.id if ctx.guild else None

	logger.info(f"User {user_id} attempting to set status interval to {seconds} seconds in guild {guild_id}")

	try:
		# Validate the interval
		if seconds < 5:
			logger.warning(f"User {user_id} tried to set invalid interval: {seconds} seconds (minimum is 5)")
			await ctx.send("❌ Interval must be at least 5 seconds")
			return

		if seconds > 3600:  # 1 hour max
			logger.warning(f"User {user_id} tried to set excessive interval: {seconds} seconds (maximum is 3600)")
			await ctx.send("❌ Interval cannot exceed 3600 seconds (1 hour)")
			return

		status_manager.set_rotation_interval(seconds)
		logger.info(f"Successfully set status rotation interval to {seconds} seconds by user {user_id}")
		await ctx.send(f"✅ Status rotation interval set to {seconds} seconds")
	except Exception as e:
		logger.error(f"Error setting status interval by user {user_id}: {e}", exc_info=True)
		await ctx.send("❌ An error occurred while setting the interval")


@bot.command(name="status_force")
@commands.has_permissions(administrator=True)
async def force_status_change(ctx):
	"""Force an immediate status change"""
	user_id = ctx.author.id
	guild_id = ctx.guild.id if ctx.guild else None

	logger.info(f"User {user_id} forcing immediate status change in guild {guild_id}")

	try:
		status = status_manager.get_weighted_random_status()
		if not status:
			logger.warning(f"No status available for forced change by user {user_id}")
			await ctx.send("❌ No status available to change to")
			return

		await status_manager.set_status(status)
		logger.info(f"Successfully forced status change by user {user_id} to: {status['type']} - {status['name']}")
		await ctx.send(f"✅ Status changed to: {status['type']} - {status['name']}")
	except Exception as e:
		logger.error(f"Error forcing status change by user {user_id}: {e}", exc_info=True)
		await ctx.send("❌ An error occurred while changing the status")


# Error handler for permission-related errors
@add_status_command.error
@remove_status_command.error
@set_interval_command.error
@force_status_change.error
async def admin_command_error(ctx, error):
	"""Handle errors for admin-only commands"""
	user_id = ctx.author.id
	command_name = ctx.command.name if ctx.command else "unknown"

	if isinstance(error, commands.MissingPermissions):
		logger.warning(f"User {user_id} attempted to use admin command '{command_name}' without permissions")
		await ctx.send("❌ You need administrator permissions to use this command")
	elif isinstance(error, commands.BadArgument):
		logger.warning(f"User {user_id} provided invalid arguments to command '{command_name}': {error}")
		await ctx.send("❌ Invalid arguments provided. Please check the command usage")
	elif isinstance(error, commands.MissingRequiredArgument):
		logger.warning(f"User {user_id} missing required arguments for command '{command_name}': {error}")
		await ctx.send("❌ Missing required arguments. Please check the command usage")
	else:
		logger.error(f"Unhandled error in command '{command_name}' by user {user_id}: {error}", exc_info=True)
		await ctx.send("❌ An unexpected error occurred")


@status_stats_command.error
async def stats_command_error(ctx, error):
	"""Handle errors for stats command"""
	user_id = ctx.author.id
	command_name = ctx.command.name if ctx.command else "unknown"

	if isinstance(error, commands.BadArgument):
		logger.warning(f"User {user_id} provided invalid arguments to command '{command_name}': {error}")
		await ctx.send("❌ Invalid arguments provided")
	else:
		logger.error(f"Unhandled error in command '{command_name}' by user {user_id}: {error}", exc_info=True)
		await ctx.send("❌ An unexpected error occurred while retrieving statistics")


# Log when commands are loaded
logger.info("IdleCommands module loaded - All status management commands registered")