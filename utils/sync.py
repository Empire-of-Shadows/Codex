import logging
import os
from pathlib import Path

from discord.ext import commands
from motor.motor_asyncio import AsyncIOMotorClient
from tabulate import tabulate
from dotenv import load_dotenv

from utils.bot import bot, s
from utils.logger import get_logger

# Load environment variables and logger setup
load_dotenv()
MONGO_URI, MONGO_URI2 = os.getenv("MONGO_URI"), os.getenv("MONGO_URI2")
logger = get_logger("Sync")


# Constants
OWNER_ID = 1264236749060575355
COG_DIRECTORIES = ["./NewMembers", "./listeners", "./games", "./commands", "./daily", "./ECOM", "./Guide", "./updates-drops", "profiles"]


@bot.command(name="load_cogs", help="Loads all cogs in the COG_DIRECTORIES list.")
@commands.is_owner()
async def load_cogs_command(ctx):
	"""
	Loads all cogs specified in the `COG_DIRECTORIES` list and sends a message pre- and post-execution.

	The command is restricted to the bot owner only. It facilitates the bot owner to dynamically
	load all defined cogs in the bot at runtime.

	:param ctx: The context in which the command was invoked
	:type ctx: commands.Context
	:return: None
	:rtype: None
	"""
	await ctx.send("Loading cogs...")
	await load_cogs()
	await ctx.send("Cogs loaded successfully.")


# Helper Functions
def log_command_details(guild_name, commands):
	"""
	Logs the details of provided commands for a specified guild. Each command's
	name, description, and type is gathered and formatted into a tabular
	representation for logging purposes. If no description exists for a command,
	a placeholder text is used.

	:param guild_name: The name of the guild for which the commands are being logged
	:param commands: A list of command objects where each command includes name,
		description, and type attributes
	:return: None
	"""
	command_data = [
		[cmd.name, cmd.description or "No description provided.", cmd.type.name]
		for cmd in commands
	]

	command_table = tabulate(
		command_data,
		headers=["Command Name", "Description", "Type"],
		tablefmt="fancy_grid"
	)
	logger.info(f"Commands for {guild_name}:\n{command_table}")


async def attach_databases():
	"""
	Attaches specific collections as bot attributes and logs the status.
	Groups successfully attached (`✅`) and failed (`❌`) attributes.
	"""
	success_logs = [f"{s}🔄 Starting database attachment process...\n"]
	failed_logs = []

	try:

		# MongoDB Client 2 - E-commerce Database
		mongo_client2 = AsyncIOMotorClient(MONGO_URI2)
		bot.db_ecom = mongo_client2["Ecom-Server"]

		result, is_success = await attach_attribute("settings", bot.db_ecom["Settings"])
		(success_logs if is_success else failed_logs).append(result)
		result, is_success = await attach_attribute("Emembers", bot.db_ecom["Users"])
		(success_logs if is_success else failed_logs).append(result)
		result, is_success = await attach_attribute("Eboosts", bot.db_ecom["Boosts"])
		(success_logs if is_success else failed_logs).append(result)
		result, is_success = await attach_attribute("power-ups", bot.db_ecom["power-ups"])
		(success_logs if is_success else failed_logs).append(result)

		# Initialize Cache Manager
		from utils.cache import create_cache_manager
		try:
			cache_manager = await create_cache_manager(MONGO_URI)
			result, is_success = await attach_attribute("cache_manager", cache_manager)
			(success_logs if is_success else failed_logs).append(result)

			# Update global cache manager reference
			import utils.cache as cache_module
			cache_module.cache_manager = cache_manager

		except Exception as cache_error:
			failed_logs.append(f"{s}❌ cache_manager → Error: {cache_error}\n")

		# Rewards Channel
		try:
			bot.rewards_channel = bot.get_channel(1395467283589107712)
			success_logs.append(f"{s}✅ rewards_channel: {bot.rewards_channel}\n")
			bot.suggest_channel = bot.get_channel(1371239792888516719)
			success_logs.append(f"{s}✅ suggest_channel: {bot.suggest_channel}\n")
			bot.admin_channel = bot.get_channel(1265125349772230787)
			success_logs.append(f"{s}✅ admin_channel: {bot.admin_channel}\n")
		except Exception as e:
			failed_logs.append(f"{s}❌ channels → Error: {e}\n")
	except Exception as e:
		failed_logs.append(f"{s}❌ Encountered a critical error during database attachment → {e}\n")

	# Add group headers for success and failure logs
	if failed_logs:
		failed_logs.insert(0, f"{s}❌ Failed to attach the following attributes:\n")
	if success_logs:
		success_logs.insert(1 if failed_logs else 0, f"{s}✅ Successfully attached the following attributes:\n")

	# Combine and log the final result
	final_log = failed_logs + success_logs
	logger.info("\n" + "".join(final_log) + f"{s}✅ Database attachment process completed.\n")


async def attach_attribute(attribute_name, attribute_value):
	"""
	Safely attaches an attribute to the bot and returns its status.
	"""
	try:
		setattr(bot, attribute_name, attribute_value)  # Attach to bot
		return f"{s}✅ {attribute_name}: {attribute_value}\n", True
	except Exception as e:
		return f"{s}❌ {attribute_name} → Error: {e}\n", False


async def load_cogs():
	"""
	Load all cogs from specified directories in `COG_DIRECTORIES`.
	Group and log successful loads (`✅`) and failed ones (`❌`) together.
	"""
	success_logs = [f"{s}🔄 Starting cog loading process...\n"]
	failed_logs = []

	for base_dir in COG_DIRECTORIES:
		for root, _, files in os.walk(base_dir):
			for file in files:
				if not file.endswith(".py") or file.startswith("__"):
					continue

				module_name = generate_cog_module_name(root, file)

				# Skip specific cases
				if module_name in bot.extensions:
					success_logs.append(f"{s}🔄 Skipping already loaded cog: {module_name}\n")
					continue

				# Safely load the cog and append to appropriate log
				result, is_success = await safely_load_cog(module_name, os.path.join(root, file))
				if is_success:
					success_logs.append(result)
				else:
					failed_logs.append(result)

	# Add summary headers and combine logs
	if failed_logs:
		failed_logs.insert(0, f"{s}❌ Failed to load the following cogs:\n")
	success_logs.append(f"{s}✅ Successfully loaded the following cogs:\n")

	# Combine and log the final output
	final_logs = failed_logs + success_logs if failed_logs else success_logs
	logger.info("\n" + "".join(final_logs) + f"{s}✅ Cog loading process completed.\n")


async def safely_load_cog(module, file_path):
	"""
	Dynamically import and load a cog module.
	Returns the result as a formatted string and a success status.
	"""
	try:
		await bot.load_extension(module)
		return f"{s}✅ {module}\n", True
	except Exception as e:
		return f"{s}❌ {module} → Error: {e}\n", False


def generate_cog_module_name(root, file):
	"""
	Helper to generate the fully qualified module name from root and file.
	"""
	# Normalize paths and remove leading "./" if present
	relative_path = os.path.relpath(os.path.join(root, file), start=str(Path("."))).replace("\\", "/")
	# Convert to Python module format
	module_name = relative_path.replace("/", ".").removesuffix(".py")
	logger.info(f"Generating module name for {file}: {module_name}")
	return module_name


def log_prefix_commands(commands):
	"""
	Logs all prefix commands with their details in a tabular format.
	"""
	command_data = [[cmd.name, cmd.help or "No description", ", ".join(cmd.aliases) or "None"] for cmd in commands]
	command_table = tabulate(command_data, headers=["Command", "Description", "Aliases"], tablefmt="fancy_grid")
	logger.info(f"Prefix Commands:\n{command_table}")


async def cache_guild_roles():
	"""Cache guild roles using the new cache manager."""
	from utils.bot import bot

	if hasattr(bot, 'cache_manager'):
		guild = bot.get_guild(1265120128295632926)
		await bot.cache_manager.cache_roles(guild)
		logger.info("Roles cached successfully.")
	else:
		logger.error("Cache manager not available")