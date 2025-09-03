import random
from datetime import datetime
from typing import Dict, Any
from IdleStatus.idle import StatusManager
from utils.logger import get_logger, log_performance, log_context

logger = get_logger("DynamicStatus")


class DynamicStatusManager(StatusManager):
	def __init__(self, config_file: str = "status_config.json"):
		logger.info(f"Initializing DynamicStatusManager with config: {config_file}")
		try:
			super().__init__(config_file)
			self.server_count = 0
			self.user_count = 0
			self.command_count = 0
			self.uptime_start = datetime.now()
			logger.info(f"DynamicStatusManager initialized successfully. Uptime started at: {self.uptime_start}")
		except Exception as e:
			logger.error(f"Failed to initialize DynamicStatusManager: {str(e)}")
			raise

	def update_stats(self, servers: int = None, users: int = None, commands: int = None):
		"""Update bot statistics for dynamic status"""
		with log_context(logger, "update_stats"):
			old_stats = {
				"servers": self.server_count,
				"users": self.user_count,
				"commands": self.command_count
			}

			if servers is not None:
				self.server_count = servers
				logger.debug(f"Server count updated: {old_stats['servers']} -> {servers}")
			if users is not None:
				self.user_count = users
				logger.debug(f"User count updated: {old_stats['users']} -> {users}")
			if commands is not None:
				self.command_count = commands
				logger.debug(f"Command count updated: {old_stats['commands']} -> {commands}")

			logger.info(
				f"Stats updated - Servers: {self.server_count}, Users: {self.user_count:,}, Commands: {self.command_count}")

	@log_performance("get_dynamic_status")
	def get_dynamic_status(self) -> dict[str, str | datetime | Any] | dict[str, datetime | Any]:
		"""Generate status with real-time data"""
		logger.debug("Generating dynamic status")

		try:
			uptime = datetime.now() - self.uptime_start
			uptime_hours = int(uptime.total_seconds() // 3600)
			logger.debug(f"Calculated uptime: {uptime_hours}h ({uptime.total_seconds():.2f}s)")

			dynamic_statuses = {
				"playing": [
					f"with {self.server_count} servers 🌐",
					f"with {self.user_count:,} users 👥",
					f"uptime: {uptime_hours}h ⏰",
				],
				"watching": [
					f"{self.server_count} servers 📊",
					f"{self.user_count:,} users online 👁️",
					f"{self.command_count} commands served 📈",
				],
				"listening": [
					f"to {self.user_count:,} users 🎧",
					f"for commands ({self.command_count} served) 🤖",
				]
			}
			logger.debug(f"Generated dynamic status options for {len(dynamic_statuses)} types")

			# Mix static and dynamic statuses
			available_status_types = list(self.status_options.keys())
			status_type = random.choice(available_status_types)
			logger.debug(f"Selected status type: {status_type} from {available_status_types}")

			# 30% chance for dynamic status if available
			use_dynamic = status_type in dynamic_statuses and random.random() < 0.3
			logger.debug(f"Using dynamic status: {use_dynamic} (type available: {status_type in dynamic_statuses})")

			if use_dynamic:
				phrase = random.choice(dynamic_statuses[status_type])
				logger.info(f"Generated dynamic status - Type: {status_type}, Phrase: {phrase}")
			else:
				# Use regular static status
				if status_type == "streaming":
					phrase = random.choice(self.status_options["streaming"]["phrases"])
					result = {
						"type": status_type,
						"name": phrase,
						"url": self.status_options["streaming"]["url"],
						"timestamp": datetime.now()
					}
					logger.info(
						f"Generated streaming status - Phrase: {phrase}, URL: {self.status_options['streaming']['url']}")
					return result
				else:
					phrase = random.choice(self.status_options[status_type])
					logger.info(f"Generated static status - Type: {status_type}, Phrase: {phrase}")

			result = {
				"type": status_type,
				"name": phrase,
				"timestamp": datetime.now()
			}

			logger.debug(f"Status generation completed successfully: {result}")
			return result

		except KeyError as e:
			logger.error(f"Missing configuration key during status generation: {str(e)}")
			logger.warning("Falling back to basic status")
			return {
				"type": "playing",
				"name": "with errors 🚫",
				"timestamp": datetime.now()
			}
		except Exception as e:
			logger.error(f"Unexpected error during status generation: {str(e)}")
			logger.warning("Falling back to basic status")
			return {
				"type": "playing",
				"name": "with issues 🔧",
				"timestamp": datetime.now()
			}