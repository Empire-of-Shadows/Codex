import random
import discord
import json
from discord.ext import tasks
from typing import Dict, List, Optional, Union
from datetime import datetime, timedelta
from utils.bot import bot
from utils.logger import get_logger

logger = get_logger("Idle")


class StatusManager:
	def __init__(self, config_file: str = "status_config.json"):
		self.config_file = config_file
		self.status_options = self._load_config()
		self.status_history = []
		self.max_history = 50
		self.weights = {"playing": 25, "watching": 25, "listening": 25, "streaming": 25}
		self.rotation_interval = 30
		self.is_running = False

	def _load_config(self) -> Dict:
		"""Load status options from config file or use defaults"""
		try:
			with open(self.config_file, 'r') as f:
				return json.load(f)
		except FileNotFoundError:
			return self._get_default_config()

	def _get_default_config(self) -> Dict:
		"""Return default status configuration"""
		return {
			"playing": [
				"with server stats ⚙️",
				"with uptime numbers ⏱️",
				"tag replies 🔖",
				"server data sync 🔄",
				"mention logs 🧾",
				"with activity reports 📈",
				"IP hunt 🔍",
				"port-checking puzzles 🧩",
				"auto-replies and help requests 🤖",
				"connection stability games 🔌",
				"ping pong with latency 🏓"
			],
			"watching": [
				"server activity 👀",
				"for mentions and replies 💬",
				"your status updates 🛰️",
				"the logs scroll endlessly 📜",
				"for API errors ⚠️",
				"network pings 📶",
				"free slots and user joins 👤",
				"your info requests 🧠",
				"the next command input ⌨️",
				"real-time usage graphs 📊",
				"the server pulse 💓"
			],
			"listening": [
				"for !help requests 🆘",
				"to server chatter 👂",
				"to the logs whisper 📝",
				"for @mentions 🔔",
				"command history talkbacks 🗣️",
				"uptime checks ⌚",
				"your support questions 🎧",
				"voice channel joins 🎙️",
				"admin alerts ⚡",
				"network heartbeat 🌐",
				"the rhythm of commands 🥁"
			],
			"streaming": {
				"phrases": [
					"server info 24/7 📡",
					"live stats and data dashboards 📺",
					"mention responses on demand 🔁",
					"command output streams 📤",
					"real-time monitoring 🎯",
					"Discord bot diagnostics 🔍",
					"user activity logs 🖥️",
					"response time tracking 🕒",
					"uptime wars: bot vs lag ⏳",
					"server history playback 📽️",
					"info queries live feed 📡"
				],
				"url": "https://yourbotinfo.url"
			}
		}

	def save_config(self):
		"""Save current status options to config file"""
		with open(self.config_file, 'w') as f:
			json.dump(self.status_options, f, indent=2)

	def add_status(self, status_type: str, phrase: str) -> bool:
		"""Add a new status phrase to a category"""
		if status_type not in self.status_options:
			return False

		if status_type == "streaming":
			self.status_options[status_type]["phrases"].append(phrase)
		else:
			self.status_options[status_type].append(phrase)

		self.save_config()
		logger.info(f"Added new {status_type} status: {phrase}")
		return True

	def remove_status(self, status_type: str, phrase: str) -> bool:
		"""Remove a status phrase from a category"""
		if status_type not in self.status_options:
			return False

		try:
			if status_type == "streaming":
				self.status_options[status_type]["phrases"].remove(phrase)
			else:
				self.status_options[status_type].remove(phrase)

			self.save_config()
			logger.info(f"Removed {status_type} status: {phrase}")
			return True
		except ValueError:
			return False

	def set_weights(self, weights: Dict[str, int]):
		"""Set probability weights for different status types"""
		total = sum(weights.values())
		self.weights = {k: (v / total) * 100 for k, v in weights.items()}
		logger.info(f"Updated status weights: {self.weights}")

	def get_weighted_random_status(self) -> Dict[str, str]:
		"""Get a random status using weighted selection and avoiding recent repeats"""
		available_types = list(self.status_options.keys())

		# Remove recently used types if history is long enough
		if len(self.status_history) >= 3:
			recent_types = [s["type"] for s in self.status_history[-3:]]
			available_types = [t for t in available_types if t not in recent_types]

		# Use weights to select type
		weights = [self.weights.get(t, 25) for t in available_types]
		status_type = random.choices(available_types, weights=weights)[0]

		# Get random phrase for selected type
		if status_type == "streaming":
			phrases = self.status_options["streaming"]["phrases"]
			random_phrase = random.choice(phrases)
			result = {
				"type": status_type,
				"name": random_phrase,
				"url": self.status_options["streaming"]["url"]
			}
		else:
			phrases = self.status_options[status_type]
			random_phrase = random.choice(phrases)
			result = {"type": status_type, "name": random_phrase}

		# Add to history
		result["timestamp"] = datetime.now()
		self.status_history.append(result)

		# Trim history
		if len(self.status_history) > self.max_history:
			self.status_history = self.status_history[-self.max_history:]

		return result

	async def set_status(self, status_data: Dict[str, str]):
		"""Set the bot's status"""
		try:
			status_type = status_data["type"]
			name = status_data["name"]

			if status_type == "playing":
				await bot.change_presence(activity=discord.Game(name=name))
			elif status_type == "watching":
				await bot.change_presence(
					activity=discord.Activity(type=discord.ActivityType.watching, name=name)
				)
			elif status_type == "listening":
				await bot.change_presence(
					activity=discord.Activity(type=discord.ActivityType.listening, name=name)
				)
			elif status_type == "streaming":
				url = status_data.get("url", "https://twitch.tv/")
				await bot.change_presence(
					activity=discord.Streaming(name=name, url=url)
				)

			logger.info(f"Status changed to {status_type}: {name}")

		except Exception as e:
			logger.error(f"Failed to set status: {e}")

	def get_status_stats(self) -> Dict:
		"""Get statistics about status usage"""
		if not self.status_history:
			return {}

		type_counts = {}
		for status in self.status_history:
			status_type = status["type"]
			type_counts[status_type] = type_counts.get(status_type, 0) + 1

		return {
			"total_changes": len(self.status_history),
			"type_distribution": type_counts,
			"last_changed": self.status_history[-1]["timestamp"] if self.status_history else None,
			"most_used_type": max(type_counts, key=type_counts.get) if type_counts else None
		}

	def set_rotation_interval(self, seconds: int):
		"""Change the rotation interval"""
		self.rotation_interval = max(10, seconds)  # Minimum 10 seconds
		if self.is_running:
			self.rotate_status.change_interval(seconds=self.rotation_interval)
		logger.info(f"Rotation interval changed to {self.rotation_interval} seconds")

	@tasks.loop(seconds=30)
	async def rotate_status(self):
		"""Rotate the bot's status"""
		try:
			random_status = self.get_weighted_random_status()
			await self.set_status(random_status)
		except Exception as e:
			logger.error(f"Error rotating status: {e}")

	def start_rotation(self):
		"""Start the status rotation"""
		if not self.is_running:
			self.rotate_status.start()
			self.is_running = True
			logger.info("Status rotation started")

	def stop_rotation(self):
		"""Stop the status rotation"""
		if self.is_running:
			self.rotate_status.stop()
			self.is_running = False
			logger.info("Status rotation stopped")


# Create global instance
status_manager = StatusManager()


# Legacy functions for backward compatibility
def get_random_status():
	return status_manager.get_weighted_random_status()


@tasks.loop(seconds=30)
async def rotate_status():
	await status_manager.rotate_status()