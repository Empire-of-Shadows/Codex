# !/usr/bin/env python3
"""
Health check script for the Discord bot container.
This script validates that the bot container is running properly.
"""

import sys
import os
import time
import logging
import requests
import psutil
from pathlib import Path

# Configure basic logging for health check
logging.basicConfig(
	level=logging.WARNING,
	format='%(asctime)s - HEALTHCHECK - %(levelname)s - %(message)s'
)
logger = logging.getLogger('healthcheck')


class HealthChecker:
	"""Health checker for the Discord bot container."""

	def __init__(self):
		self.max_check_time = 8.0

	def check_main_process(self):
		"""Check if the main bot process is running."""
		try:
			# Look for python processes running codex.py
			for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
				try:
					if proc.info['name'] == 'python' and proc.info['cmdline']:
						cmdline = ' '.join(proc.info['cmdline'])
						if 'codex.py' in cmdline:
							logger.info(f"Found main bot process: PID {proc.info['pid']}")
							return True
				except (psutil.NoSuchProcess, psutil.AccessDenied):
					continue

			logger.error("Main bot process not found")
			return False

		except Exception as e:
			logger.error(f"Process check failed: {e}")
			return False

	def check_log_directory(self):
		"""Check if log directory exists and is writable."""
		try:
			log_dir = Path("/app/logs")
			if not log_dir.exists():
				log_dir.mkdir(parents=True, exist_ok=True)

			# Test write access
			test_file = log_dir / "healthcheck_test"
			test_file.write_text("health_check")
			test_file.unlink()

			return True
		except Exception as e:
			logger.error(f"Log directory check failed: {e}")
			return False

	def check_recent_logs(self):
		"""Check if bot is generating recent log entries."""
		try:
			log_dir = Path("/app/logs")
			if not log_dir.exists():
				return False

			# Look for recent log files (within last 5 minutes)
			recent_threshold = time.time() - 300  # 5 minutes

			for log_file in log_dir.glob("*.log"):
				if log_file.stat().st_mtime > recent_threshold:
					# Check if file has recent content
					if log_file.stat().st_size > 0:
						return True

			logger.warning("No recent log activity found")
			return False

		except Exception as e:
			logger.error(f"Log activity check failed: {e}")
			return False

	def check_memory_usage(self):
		"""Check memory usage of the container."""
		try:
			# Get memory info for all python processes
			total_memory_mb = 0
			process_count = 0

			for proc in psutil.process_iter(['pid', 'name', 'memory_info']):
				try:
					if proc.info['name'] == 'python':
						memory_mb = proc.info['memory_info'].rss / 1024 / 1024
						total_memory_mb += memory_mb
						process_count += 1
				except (psutil.NoSuchProcess, psutil.AccessDenied):
					continue

			logger.info(f"Python processes: {process_count}, Total memory: {total_memory_mb:.1f}MB")

			# Alert if memory usage is above 500MB
			if total_memory_mb > 500:
				logger.warning(f"High memory usage: {total_memory_mb:.1f}MB")

			return True

		except Exception as e:
			logger.error(f"Memory check failed: {e}")
			return False

	def check_discord_api_connectivity(self):
		"""Check if Discord API is reachable."""
		try:
			response = requests.get(
				"https://discord.com/api/v10/gateway",
				timeout=5
			)

			if response.status_code == 200:
				return True
			else:
				logger.error(f"Discord API returned status: {response.status_code}")
				return False

		except requests.RequestException as e:
			logger.error(f"Discord API connectivity check failed: {e}")
			return False

	def run_health_check(self):
		"""Run comprehensive health check."""
		start_time = time.time()

		try:
			checks_passed = 0
			total_checks = 5

			# Check 1: Main process
			if self.check_main_process():
				checks_passed += 1

			# Check 2: Log directory
			if self.check_log_directory():
				checks_passed += 1

			# Check 3: Recent log activity
			if self.check_recent_logs():
				checks_passed += 1

			# Check 4: Memory usage
			if self.check_memory_usage():
				checks_passed += 1

			# Check 5: Discord API connectivity
			if self.check_discord_api_connectivity():
				checks_passed += 1

			# Check response time
			elapsed = time.time() - start_time
			if elapsed > self.max_check_time:
				logger.error(f"Health check took too long: {elapsed:.2f}s")
				return False

			# Require at least 4/5 checks to pass
			success_threshold = 4
			is_healthy = checks_passed >= success_threshold

			logger.info(f"Health check completed: {checks_passed}/{total_checks} checks passed in {elapsed:.2f}s")

			return is_healthy

		except Exception as e:
			logger.error(f"Health check failed with exception: {e}")
			return False


def main():
	"""Main health check function."""
	health_checker = HealthChecker()

	try:
		is_healthy = health_checker.run_health_check()

		if is_healthy:
			print("HEALTHY: All critical checks passed")
			sys.exit(0)
		else:
			print("UNHEALTHY: One or more critical checks failed")
			sys.exit(1)

	except Exception as e:
		print(f"UNHEALTHY: Health check error: {e}")
		sys.exit(1)


if __name__ == "__main__":
	main()