import os
import logging
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
MONGO_URI2 = os.getenv("MONGO_URI2")

logger = logging.getLogger("DatabaseManager")


class DatabaseManager:
	"""Shared database manager for profile-related operations."""

	def __init__(self):
		self.db_client = None
		self.users = None
		self.inventory = None
		self.user_stats = None
		self._initialized = False

	async def initialize(self):
		"""Initialize database connections."""
		if self._initialized:
			return

		try:
			logger.info("Connecting to MongoDB...")
			self.db_client = AsyncIOMotorClient(
				MONGO_URI2,
				maxPoolSize=50,
				minPoolSize=10,
				maxIdleTimeMS=30000,
				serverSelectionTimeoutMS=5000,
			)
			self.users = self.db_client["Server_Data"]["servers.members"]
			self.user_stats = self.db_client["Ecom-Server"]["Users"]
			self.inventory = self.db_client["Ecom-Server"]["Inventory"]
			self._initialized = True
			logger.info("Successfully connected to MongoDB and initialized collections.")
		except Exception as e:
			logger.error(f"Failed to connect to MongoDB: {e}")
			raise

	async def close(self):
		"""Close database connections."""
		if self.db_client:
			self.db_client.close()
			self._initialized = False


# Global database manager instance
db_manager = DatabaseManager()