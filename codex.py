import asyncio
import os

import discord
from tabulate import tabulate
import logging
from utils.bot import bot, TOKEN, s
from utils.logger import get_logger, setup_application_logging
from utils.sync import load_cogs, attach_databases
from IdleStatus.idle import rotate_status

# Initialize application-wide logging
APPLICATION_NAME = "discord-bot-codex"
app_logger = setup_application_logging(
	app_name=APPLICATION_NAME,
	log_level=logging.INFO,  # Change to DEBUG for development
	log_dir="logs",
	enable_performance_logging=True,
	max_file_size=20 * 1024 * 1024,  # 20 MB
	backup_count=10
)

# Main logger for this module
logger = get_logger("main")
MY_TEST_GUILD_ID = 1265120128295632926  # Replace with your test guild ID


async def on_ready():
	"""
    Handles the bot's readiness state and performs initialization tasks when the bot is ready.

    This function logs the bot's login status and executes necessary steps to synchronize the bot,
    initialize services, load extensions, synchronize command trees, and start background tasks
    like status rotation. It ensures that the bot is properly configured and operational.

    :raises Exception: If an error occurs during bot synchronization, extension loading, or
                       initializing background services.
    """
	logger.info(f"Bot logged in as {bot.user}")
	logger.info(f"Bot ID: {bot.user.id}")
	logger.info(f"Connected to {len(bot.guilds)} guilds")

	startup_start = time.perf_counter()

	try:
		# Database attachment phase
		db_start = time.perf_counter()
		try:
			await attach_databases()
			db_time = time.perf_counter() - db_start
			logger.info(f"Database attachment completed in {db_time:.2f}s")
		except Exception as attaching_error:
			logger.fatal(f"Error during database attachment: {attaching_error}", exc_info=True)
			return

		# Cog loading phase
		cog_start = time.perf_counter()
		await load_cogs()
		cog_time = time.perf_counter() - cog_start
		logger.info(f"Cog loading completed in {cog_time:.2f}s")

		# Command synchronization phase
		sync_start = time.perf_counter()
		try:
			synced_global = await bot.tree.sync()
			sync_time = time.perf_counter() - sync_start
			logger.info(f"Command synchronization completed in {sync_time:.2f}s")
			logger.info(f"Synchronized {len(synced_global)} global slash commands")
		except Exception as e:
			logger.error(f"Error during command synchronization: {e}", exc_info=True)
			raise

		# Status and presence setup
		status_start = time.perf_counter()
		try:
			await bot.change_presence(status=discord.Status.online)
			rotate_status.start()
			status_time = time.perf_counter() - status_start
			logger.info(f"Status rotation initialized in {status_time:.2f}s")
		except Exception as e:
			logger.error(f"Error initializing status rotation: {e}", exc_info=True)

		# Log all commands in a structured format
		await log_all_commands()

		# Log final startup metrics
		total_startup_time = time.perf_counter() - startup_start
		logger.info(f"Bot startup completed successfully in {total_startup_time:.2f}s")
		logger.info("=" * 60)
		logger.info("BOT IS NOW ONLINE AND READY")
		logger.info("=" * 60)

	except Exception as e:
		logger.error(f"Critical error during bot initialization: {e}", exc_info=True)
		raise


bot.event(on_ready)  # Register the event


async def log_all_commands():
	"""
    Logs all commands (prefix and slash) in a structured table format.
    """
	try:
		# Log prefix commands
		prefix_commands = [
			[cmd.name, cmd.help or "No description provided", ", ".join(cmd.aliases) or "None"]
			for cmd in bot.commands
		]

		if prefix_commands:
			prefix_table = tabulate(
				prefix_commands,
				headers=["Prefix Command", "Description", "Aliases"],
				tablefmt="fancy_grid"
			)
			logger.info(f"Registered Prefix Commands ({len(prefix_commands)}):\n{prefix_table}")
		else:
			logger.info("No prefix commands registered")

		# Log slash commands
		slash_commands = [
			[cmd.name, cmd.description or "No description provided", cmd.parent.name if cmd.parent else "N/A"]
			for cmd in bot.tree.get_commands()
		]

		if slash_commands:
			slash_table = tabulate(
				slash_commands,
				headers=["Slash Command", "Description", "Parent Command (Group)"],
				tablefmt="fancy_grid"
			)
			logger.info(f"Registered Slash Commands ({len(slash_commands)}):\n{slash_table}")
		else:
			logger.info("No slash commands registered")

	except Exception as e:
		logger.error(f"Error logging command information: {e}", exc_info=True)


async def shutdown_handler():
	"""
    Handles the shutdown process for the application, ensuring a graceful cleanup
    of resources and proper termination of running tasks. This function specifically
    handles stopping any background tasks (e.g., status rotation) and closing the
    bot connection.

    :return: None
    :rtype: None
    """
	logger.info("Initiating graceful shutdown...")
	shutdown_start = time.perf_counter()

	# Stop status rotation
	try:
		if rotate_status.is_running():
			rotate_status.cancel()
			logger.info("Status rotation task stopped successfully")
		else:
			logger.debug("Status rotation task was not running")
	except Exception as e:
		logger.error(f"Error stopping status rotation: {e}", exc_info=True)

	# Close bot connection
	try:
		if not bot.is_closed():
			await bot.close()
			logger.info("Bot connection closed successfully")
		else:
			logger.debug("Bot connection was already closed")
	except Exception as shutdown_error:
		logger.error(f"Error during bot shutdown: {shutdown_error}", exc_info=True)

	# Log shutdown metrics
	shutdown_time = time.perf_counter() - shutdown_start
	logger.info(f"Shutdown completed in {shutdown_time:.2f}s")
	logger.info("Application terminated")


async def start_services():
	"""
    Starts the services required for the application, including logging configuration
    and initializing bots. This function handles asynchronous tasks and ensures any
    errors are logged properly. It also ensures a graceful shutdown when an
    exception is raised.

    :raises Exception: Raises any exception encountered to ensure proper shutdown.
    :return: None
    """
	logger.info(f"Starting {APPLICATION_NAME} services...")
	logger.info(f"Python version: {os.sys.version}")
	logger.info(f"Discord.py version: {discord.__version__}")

	service_start = time.perf_counter()

	try:
		# Log environment information
		logger.debug(f"Working directory: {os.getcwd()}")
		logger.debug(f"Log directory: logs/")

		# Start the bot
		logger.info("Starting Discord bot...")
		bot_task = asyncio.create_task(bot.start(TOKEN))

		# Add additional services here as needed
		logger.debug("All services initialized, waiting for completion...")

		await asyncio.gather(bot_task)

	except asyncio.CancelledError:
		logger.info("Service startup was cancelled")
		raise
	except Exception as e:
		service_time = time.perf_counter() - service_start
		logger.error(f"Critical error in services after {service_time:.2f}s: {e}", exc_info=True)
		raise
	finally:
		await shutdown_handler()


if __name__ == "__main__":
	"""
    Main entry point for the application. This function initializes the application
    and handles top-level exceptions and signals.
    """
	import logging
	import time
	import signal
	import sys


	# Set up signal handlers for graceful shutdown
	def signal_handler(signum, frame):
		logger.info(f"Received signal {signum}, initiating shutdown...")
		sys.exit(0)


	signal.signal(signal.SIGINT, signal_handler)
	signal.signal(signal.SIGTERM, signal_handler)

	try:
		logger.info(f"=== Starting {APPLICATION_NAME} ===")
		asyncio.run(start_services())
	except KeyboardInterrupt:
		logger.info("Received keyboard interrupt signal")
	except SystemExit:
		logger.info("Received system exit signal")
	except Exception as e:
		logger.critical(f"Fatal error occurred: {e}", exc_info=True)
		sys.exit(1)
	finally:
		logger.info(f"=== {APPLICATION_NAME} shutdown complete ===")