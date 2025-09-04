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

# Configure basic logging for health check with both file and console output
logging.basicConfig(
	level=logging.INFO,
	format='%(asctime)s - HEALTHCHECK - %(levelname)s - %(message)s',
	handlers=[
		logging.StreamHandler(sys.stdout),
		logging.StreamHandler(sys.stderr)
	]
)
logger = logging.getLogger('healthcheck')



class HealthChecker:
	"""Health checker for the Discord bot container."""

	def __init__(self):
		self.max_check_time = 8.0

	def check_main_process(self):
		"""Check if the main bot process is running."""
		try:
			# Look for python processes running codex.py or any python process in /app
			main_process_found = False
			python_processes = []

			for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cwd']):
				try:
					# Skip if not a python process
					if not proc.info['name'] or 'python' not in proc.info['name'].lower():
						continue

					cmdline_str = ' '.join(proc.info['cmdline']) if proc.info['cmdline'] else ''
					python_processes.append({
						'pid': proc.info['pid'],
						'cmdline': cmdline_str,
						'cwd': proc.info.get('cwd', 'N/A')
					})

					# Check for codex.py specifically
					if 'codex.py' in cmdline_str:
						logger.info(f"Found main bot process: PID {proc.info['pid']} - {cmdline_str}")
						main_process_found = True
					# Also accept any python process running from /app directory
					elif '/app' in cmdline_str or (proc.info.get('cwd') and '/app' in proc.info['cwd']):
						logger.info(f"Found potential bot process: PID {proc.info['pid']} - {cmdline_str}")
						main_process_found = True

				except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
					continue

			# Log all python processes for debugging
			if python_processes:
				logger.info(f"Found {len(python_processes)} Python processes:")
				for proc_info in python_processes:
					logger.info(f"  PID {proc_info['pid']}: {proc_info['cmdline']} (cwd: {proc_info['cwd']})")
			else:
				logger.warning("No Python processes found")

			if not main_process_found:
				logger.error("Main bot process not found")

			return main_process_found

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
			check_results = {}

			# Check 1: Main process
			try:
				result = self.check_main_process()
				check_results['main_process'] = result
				if result:
					checks_passed += 1
			except Exception as e:
				logger.error(f"Main process check failed: {e}")
				check_results['main_process'] = False

			# Check 2: Log directory
			try:
				result = self.check_log_directory()
				check_results['log_directory'] = result
				if result:
					checks_passed += 1
			except Exception as e:
				logger.error(f"Log directory check failed: {e}")
				check_results['log_directory'] = False

			# Check 3: Recent log activity (make this optional)
			try:
				result = self.check_recent_logs()
				check_results['recent_logs'] = result
				if result:
					checks_passed += 1
			except Exception as e:
				logger.warning(f"Recent logs check failed (non-critical): {e}")
				check_results['recent_logs'] = False

			# Check 4: Memory usage
			try:
				result = self.check_memory_usage()
				check_results['memory_usage'] = result
				if result:
					checks_passed += 1
			except Exception as e:
				logger.warning(f"Memory usage check failed: {e}")
				check_results['memory_usage'] = False

			# Check 5: Discord API connectivity
			try:
				result = self.check_discord_api_connectivity()
				check_results['discord_api'] = result
				if result:
					checks_passed += 1
			except Exception as e:
				logger.warning(f"Discord API check failed: {e}")
				check_results['discord_api'] = False

			# Check response time
			elapsed = time.time() - start_time
			if elapsed > self.max_check_time:
				logger.error(f"Health check took too long: {elapsed:.2f}s")
				return False

			# Lower the success threshold - require at least 3/5 checks to pass
			# Main process + log directory + one other check should be sufficient
			success_threshold = 3
			is_healthy = checks_passed >= success_threshold

			# Log detailed results
			logger.info(f"Health check completed in {elapsed:.2f}s:")
			for check_name, result in check_results.items():
				status = "PASS" if result else "FAIL"
				logger.info(f"  {check_name}: {status}")

			logger.info(f"Overall: {checks_passed}/{total_checks} checks passed (threshold: {success_threshold})")

			# Special case: If main process is not found but other checks pass, still fail
			if not check_results.get('main_process', False):
				logger.error("Main process check failed - marking health check as failed regardless of other checks")
				return False

			return is_healthy

		except Exception as e:
			logger.error(f"Health check failed with exception: {e}")
			return False


def main():
	"""Main health check function."""
	print("Health check main() function started", flush=True)
	logger.info("Starting health check process")

	health_checker = HealthChecker()

	try:
		print("Running health check...", flush=True)
		is_healthy = health_checker.run_health_check()

		if is_healthy:
			print("HEALTHY: All critical checks passed", flush=True)
			logger.info("Health check PASSED")
			sys.exit(0)
		else:
			print("UNHEALTHY: One or more critical checks failed", flush=True)
			logger.error("Health check FAILED")
			sys.exit(1)

	except Exception as e:
		error_msg = f"UNHEALTHY: Health check error: {e}"
		print(error_msg, flush=True)
		logger.error(error_msg)
		sys.exit(1)


if __name__ == "__main__":
	main()