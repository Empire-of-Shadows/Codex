import os
import logging
import asyncio
from typing import Dict, List, Optional, Any, Union, Callable
from datetime import datetime, timedelta
from dataclasses import dataclass

import pytz
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase, AsyncIOMotorCollection
from pymongo import UpdateOne, InsertOne, DeleteOne, ReplaceOne, IndexModel
from pymongo.errors import BulkWriteError, ConnectionFailure, OperationFailure
from dotenv import load_dotenv
import backoff
from functools import wraps
import time
from collections import defaultdict

# Load environment variables
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")
MONGO_URI2 = os.getenv("MONGO_URI2")

logger = logging.getLogger("DatabaseManager")


@dataclass
class CollectionConfig:
	"""Configuration for a collection including indexes and settings."""
	name: str
	database: str
	connection: str = 'primary'
	indexes: List[IndexModel] = None
	capped: bool = False
	max_size: int = None
	max_documents: int = None



class ConnectionPool:
	"""Manages MongoDB connection pooling and health monitoring."""

	def __init__(self, uri: str, pool_config: Dict[str, Any] = None, connection_name: str = "default"):
		self.uri = uri
		self.connection_name = connection_name
		self.config = pool_config or {
			'maxPoolSize': 100,
			'minPoolSize': 10,
			'maxIdleTimeMS': 30000,
			'serverSelectionTimeoutMS': 5000,
			'connectTimeoutMS': 10000,
			'socketTimeoutMS': 20000,
			'retryWrites': True,
			'retryReads': True
		}
		self.client: Optional[AsyncIOMotorClient] = None
		self._health_check_interval = 30  # seconds
		self._last_health_check = 0

	async def initialize(self) -> AsyncIOMotorClient:
		"""Initialize the connection pool."""
		if self.client is None:
			logger.info(f"Initializing MongoDB connection pool for {self.connection_name}...")
			self.client = AsyncIOMotorClient(self.uri, **self.config)
			await self._health_check()
			logger.info(f"MongoDB connection pool for {self.connection_name} initialized successfully")
		return self.client

	async def _health_check(self):
		"""Perform health check on the connection."""
		try:
			await self.client.admin.command('ping')
			self._last_health_check = time.time()
			logger.debug(f"MongoDB health check passed for {self.connection_name}")
		except Exception as e:
			logger.error(f"MongoDB health check failed for {self.connection_name}: {e}")
			raise ConnectionFailure(f"Database health check failed for {self.connection_name}")

	async def get_client(self) -> AsyncIOMotorClient:
		"""Get a healthy client connection."""
		if self.client is None:
			await self.initialize()

		# Perform periodic health checks
		if time.time() - self._last_health_check > self._health_check_interval:
			await self._health_check()

		return self.client

	async def close(self):
		"""Close the connection pool."""
		if self.client:
			self.client.close()
			self.client = None
			logger.info(f"MongoDB connection pool for {self.connection_name} closed")



def with_retry(max_retries: int = 3, backoff_factor: float = 1.0):
	"""Decorator for database operations with exponential backoff retry."""

	def decorator(func):
		@wraps(func)
		async def wrapper(*args, **kwargs):
			@backoff.on_exception(
				backoff.expo,
				(ConnectionFailure, OperationFailure),
				max_tries=max_retries,
				factor=backoff_factor,
				jitter=backoff.random_jitter
			)
			async def _execute():
				return await func(*args, **kwargs)

			return await _execute()

		return wrapper

	return decorator


class CollectionManager:
	"""Manages CRUD operations for a specific collection with caching and optimization."""

	def __init__(self, collection: AsyncIOMotorCollection, config: CollectionConfig):
		self.collection = collection
		self.config = config
		self.name = config.name
		self._cache: Dict[str, Any] = {}
		self._cache_ttl: Dict[str, float] = {}
		self._default_cache_duration = 300  # 5 minutes

	# CREATE Operations

	@with_retry(max_retries=3)
	async def create_one(self, document: Dict[str, Any], **kwargs) -> Any:
		"""
		Insert a single document.

		Args:
			document: The document to insert
			**kwargs: Additional options for insert_one

		Returns:
			The inserted document's ID
		"""
		try:
			document['created_at'] = datetime.now(tz=pytz.UTC)
			document['updated_at'] = datetime.now(tz=pytz.UTC)

			result = await self.collection.insert_one(document, **kwargs)
			logger.debug(f"Inserted document with ID {result.inserted_id} into {self.name}")

			# Invalidate relevant caches
			self._invalidate_cache()

			return result.inserted_id
		except Exception as e:
			logger.error(f"Error creating document in {self.name}: {e}")
			raise

	@with_retry(max_retries=3)
	async def create_many(self, documents: List[Dict[str, Any]],
						  ordered: bool = False, **kwargs) -> List[Any]:
		"""
		Insert multiple documents with bulk operations.

		Args:
			documents: List of documents to insert
			ordered: Whether to perform ordered inserts
			**kwargs: Additional options for insert_many

		Returns:
			List of inserted document IDs
		"""
		if not documents:
			return []

		try:
			# Add timestamps to all documents
			now = datetime.now(tz=pytz.UTC)
			for doc in documents:
				doc['created_at'] = now
				doc['updated_at'] = now

			result = await self.collection.insert_many(documents, ordered=ordered, **kwargs)
			logger.debug(f"Inserted {len(result.inserted_ids)} documents into {self.name}")

			# Invalidate relevant caches
			self._invalidate_cache()

			return result.inserted_ids
		except BulkWriteError as bwe:
			logger.error(f"Bulk write error in {self.name}: {bwe.details}")
			# Return successfully inserted IDs even on partial failure
			return [oid for oid in bwe.details.get('insertedIds', {}).values()]
		except Exception as e:
			logger.error(f"Error creating documents in {self.name}: {e}")
			raise

	# READ Operations

	@with_retry(max_retries=2)
	async def find_one(self, filter_dict: Dict[str, Any] = None,
					   projection: Dict[str, Any] = None,
					   cache_key: str = None,
					   cache_duration: int = None,
					   **kwargs) -> Optional[Dict[str, Any]]:
		"""
		Find a single document with optional caching.

		Args:
			filter_dict: Query filter
			projection: Fields to include/exclude
			cache_key: Key for caching the result
			cache_duration: Cache duration in seconds
			**kwargs: Additional options for find_one

		Returns:
			The found document or None
		"""
		# Check cache first
		if cache_key and self._is_cached(cache_key):
			logger.debug(f"Cache hit for {cache_key} in {self.name}")
			return self._get_cached(cache_key)

		try:
			filter_dict = filter_dict or {}
			result = await self.collection.find_one(filter_dict, projection, **kwargs)

			# Cache the result if cache_key is provided
			if cache_key and result:
				duration = cache_duration or self._default_cache_duration
				self._set_cache(cache_key, result, duration)

			return result
		except Exception as e:
			logger.error(f"Error finding document in {self.name}: {e}")
			raise

	@with_retry(max_retries=2)
	async def find_many(self, filter_dict: Dict[str, Any] = None,
						projection: Dict[str, Any] = None,
						sort: List[tuple] = None,
						limit: int = None,
						skip: int = 0,
						**kwargs) -> List[Dict[str, Any]]:
		"""
		Find multiple documents with cursor optimization.

		Args:
			filter_dict: Query filter
			projection: Fields to include/exclude
			sort: Sort specification
			limit: Maximum number of documents
			skip: Number of documents to skip
			**kwargs: Additional options for find

		Returns:
			List of found documents
		"""
		try:
			filter_dict = filter_dict or {}
			cursor = self.collection.find(filter_dict, projection, **kwargs)

			if sort:
				cursor = cursor.sort(sort)
			if skip > 0:
				cursor = cursor.skip(skip)
			if limit:
				cursor = cursor.limit(limit)

			documents = await cursor.to_list(length=limit)
			logger.debug(f"Found {len(documents)} documents in {self.name}")

			return documents
		except Exception as e:
			logger.error(f"Error finding documents in {self.name}: {e}")
			raise

	@with_retry(max_retries=2)
	async def count_documents(self, filter_dict: Dict[str, Any] = None, **kwargs) -> int:
		"""
		Count documents matching the filter.

		Args:
			filter_dict: Query filter
			**kwargs: Additional options for count_documents

		Returns:
			Number of matching documents
		"""
		try:
			filter_dict = filter_dict or {}
			count = await self.collection.count_documents(filter_dict, **kwargs)
			return count
		except Exception as e:
			logger.error(f"Error counting documents in {self.name}: {e}")
			raise

	@with_retry(max_retries=2)
	async def aggregate(self, pipeline: List[Dict[str, Any]], **kwargs) -> List[Dict[str, Any]]:
		"""
		Perform aggregation pipeline operations.

		Args:
			pipeline: Aggregation pipeline
			**kwargs: Additional options for aggregate

		Returns:
			List of aggregation results
		"""
		try:
			cursor = self.collection.aggregate(pipeline, **kwargs)
			results = await cursor.to_list(length=None)
			logger.debug(f"Aggregation returned {len(results)} results from {self.name}")
			return results
		except Exception as e:
			logger.error(f"Error in aggregation for {self.name}: {e}")
			raise

	# UPDATE Operations

	@with_retry(max_retries=3)
	async def update_one(self, filter_dict: Dict[str, Any],
						 update_dict: Dict[str, Any],
						 upsert: bool = False,
						 **kwargs) -> bool:
		"""
		Update a single document.

		Args:
			filter_dict: Query filter
			update_dict: Update operations
			upsert: Whether to insert if no document matches
			**kwargs: Additional options for update_one

		Returns:
			True if document was modified, False otherwise
		"""
		try:
			# Add updated_at timestamp
			if '$set' not in update_dict:
				update_dict['$set'] = {}
			update_dict['$set']['updated_at'] = datetime.now(tz=pytz.UTC)

			result = await self.collection.update_one(filter_dict, update_dict,
													  upsert=upsert, **kwargs)

			success = result.modified_count > 0 or (upsert and result.upserted_id is not None)
			if success:
				logger.debug(f"Updated document in {self.name}")
				self._invalidate_cache()

			return success
		except Exception as e:
			logger.error(f"Error updating document in {self.name}: {e}")
			raise

	@with_retry(max_retries=3)
	async def update_many(self, filter_dict: Dict[str, Any],
						  update_dict: Dict[str, Any],
						  **kwargs) -> int:
		"""
		Update multiple documents.

		Args:
			filter_dict: Query filter
			update_dict: Update operations
			**kwargs: Additional options for update_many

		Returns:
			Number of documents modified
		"""
		try:
			# Add updated_at timestamp
			if '$set' not in update_dict:
				update_dict['$set'] = {}
			update_dict['$set']['updated_at'] = datetime.now(tz=pytz.UTC)

			result = await self.collection.update_many(filter_dict, update_dict, **kwargs)

			if result.modified_count > 0:
				logger.debug(f"Updated {result.modified_count} documents in {self.name}")
				self._invalidate_cache()

			return result.modified_count
		except Exception as e:
			logger.error(f"Error updating documents in {self.name}: {e}")
			raise

	@with_retry(max_retries=3)
	async def replace_one(self, filter_dict: Dict[str, Any],
						  replacement: Dict[str, Any],
						  upsert: bool = False,
						  **kwargs) -> bool:
		"""
		Replace a single document.

		Args:
			filter_dict: Query filter
			replacement: Replacement document
			upsert: Whether to insert if no document matches
			**kwargs: Additional options for replace_one

		Returns:
			True if document was replaced, False otherwise
		"""
		try:
			# Add timestamps to replacement
			replacement['updated_at'] = datetime.now(tz=pytz.UTC)
			if 'created_at' not in replacement:
				replacement['created_at'] = datetime.now(tz=pytz.UTC)

			result = await self.collection.replace_one(filter_dict, replacement,
													   upsert=upsert, **kwargs)

			success = result.modified_count > 0 or (upsert and result.upserted_id is not None)
			if success:
				logger.debug(f"Replaced document in {self.name}")
				self._invalidate_cache()

			return success
		except Exception as e:
			logger.error(f"Error replacing document in {self.name}: {e}")
			raise

	# DELETE Operations

	@with_retry(max_retries=3)
	async def delete_one(self, filter_dict: Dict[str, Any], **kwargs) -> bool:
		"""
		Delete a single document.

		Args:
			filter_dict: Query filter
			**kwargs: Additional options for delete_one

		Returns:
			True if document was deleted, False otherwise
		"""
		try:
			result = await self.collection.delete_one(filter_dict, **kwargs)

			if result.deleted_count > 0:
				logger.debug(f"Deleted document from {self.name}")
				self._invalidate_cache()
				return True

			return False
		except Exception as e:
			logger.error(f"Error deleting document from {self.name}: {e}")
			raise

	@with_retry(max_retries=3)
	async def delete_many(self, filter_dict: Dict[str, Any], **kwargs) -> int:
		"""
		Delete multiple documents.

		Args:
			filter_dict: Query filter
			**kwargs: Additional options for delete_many

		Returns:
			Number of documents deleted
		"""
		try:
			result = await self.collection.delete_many(filter_dict, **kwargs)

			if result.deleted_count > 0:
				logger.debug(f"Deleted {result.deleted_count} documents from {self.name}")
				self._invalidate_cache()

			return result.deleted_count
		except Exception as e:
			logger.error(f"Error deleting documents from {self.name}: {e}")
			raise

	# BULK Operations

	@with_retry(max_retries=3)
	async def bulk_write(self, operations: List[Union[UpdateOne, InsertOne, DeleteOne, ReplaceOne]],
						 ordered: bool = False, **kwargs) -> Dict[str, Any]:
		"""
		Perform bulk write operations for maximum efficiency.

		Args:
			operations: List of bulk operations
			ordered: Whether to perform operations in order
			**kwargs: Additional options for bulk_write

		Returns:
			Dictionary with operation results
		"""
		if not operations:
			return {'inserted_count': 0, 'modified_count': 0, 'deleted_count': 0}

		try:
			# Add timestamps to operations where applicable
			now = datetime.now(tz=pytz.UTC)
			for op in operations:
				if isinstance(op, (UpdateOne, ReplaceOne)):
					if hasattr(op, '_update') and isinstance(op._update, dict):
						if '$set' not in op._update:
							op._update['$set'] = {}
						op._update['$set']['updated_at'] = now
				elif isinstance(op, InsertOne):
					if hasattr(op, '_doc') and isinstance(op._doc, dict):
						op._doc['created_at'] = now
						op._doc['updated_at'] = now

			result = await self.collection.bulk_write(operations, ordered=ordered, **kwargs)

			logger.debug(f"Bulk operation completed on {self.name}: "
						 f"inserted={result.inserted_count}, "
						 f"modified={result.modified_count}, "
						 f"deleted={result.deleted_count}")

			# Invalidate cache if any modifications occurred
			if result.inserted_count > 0 or result.modified_count > 0 or result.deleted_count > 0:
				self._invalidate_cache()

			return {
				'inserted_count': result.inserted_count,
				'modified_count': result.modified_count,
				'deleted_count': result.deleted_count,
				'upserted_count': result.upserted_count,
				'upserted_ids': result.upserted_ids
			}
		except BulkWriteError as bwe:
			logger.warning(f"Bulk write error in {self.name}: {bwe.details}")
			# Return partial results
			result = bwe.details
			return {
				'inserted_count': result.get('nInserted', 0),
				'modified_count': result.get('nModified', 0),
				'deleted_count': result.get('nRemoved', 0),
				'upserted_count': result.get('nUpserted', 0),
				'errors': result.get('writeErrors', [])
			}
		except Exception as e:
			logger.error(f"Error in bulk write for {self.name}: {e}")
			raise

	# UTILITY Methods

	async def create_indexes(self) -> List[str]:
		"""Create indexes defined in the collection configuration."""
		if not self.config.indexes:
			return []

		try:
			index_names = await self.collection.create_indexes(self.config.indexes)
			logger.info(f"Created {len(index_names)} indexes for {self.name}: {index_names}")
			return index_names
		except Exception as e:
			logger.error(f"Error creating indexes for {self.name}: {e}")
			raise

	async def drop_indexes(self, index_names: List[str] = None):
		"""Drop specified indexes or all non-default indexes."""
		try:
			if index_names:
				for index_name in index_names:
					await self.collection.drop_index(index_name)
				logger.info(f"Dropped indexes {index_names} from {self.name}")
			else:
				await self.collection.drop_indexes()
				logger.info(f"Dropped all indexes from {self.name}")
		except Exception as e:
			logger.error(f"Error dropping indexes from {self.name}: {e}")
			raise

	async def get_stats(self) -> Dict[str, Any]:
		"""Get collection statistics."""
		try:
			stats = await self.collection.database.command('collStats', self.name)
			return {
				'count': stats.get('count', 0),
				'size': stats.get('size', 0),
				'avgObjSize': stats.get('avgObjSize', 0),
				'storageSize': stats.get('storageSize', 0),
				'indexes': stats.get('nindexes', 0),
				'totalIndexSize': stats.get('totalIndexSize', 0)
			}
		except Exception as e:
			logger.warning(f"Error getting stats for {self.name}: {e}")
			return {}

	# CACHE Management

	def _is_cached(self, key: str) -> bool:
		"""Check if a key is cached and not expired."""
		if key not in self._cache:
			return False

		if key in self._cache_ttl and time.time() > self._cache_ttl[key]:
			del self._cache[key]
			del self._cache_ttl[key]
			return False

		return True

	def _get_cached(self, key: str) -> Any:
		"""Get a cached value."""
		return self._cache.get(key)

	def _set_cache(self, key: str, value: Any, duration: int):
		"""Set a cached value with TTL."""
		self._cache[key] = value
		self._cache_ttl[key] = time.time() + duration

	def _invalidate_cache(self, pattern: str = None):
		"""Invalidate cache entries, optionally matching a pattern."""
		if pattern:
			keys_to_remove = [k for k in self._cache.keys() if pattern in k]
			for key in keys_to_remove:
				self._cache.pop(key, None)
				self._cache_ttl.pop(key, None)
		else:
			self._cache.clear()
			self._cache_ttl.clear()


class DatabaseManager:
	"""
	Comprehensive MongoDB database manager with connection pooling,
	CRUD operations, caching, error handling, and performance optimization.
	"""

	def __init__(self, primary_uri: str = None, secondary_uri: str = None):
		self.primary_uri = primary_uri or MONGO_URI
		self.secondary_uri = secondary_uri or MONGO_URI2

		if not self.primary_uri:
			raise ValueError("Primary MongoDB URI not provided")

		# Create connection pools for each URI
		self.connection_pools = {}
		self.connection_pools['primary'] = ConnectionPool(self.primary_uri, connection_name='primary')

		if self.secondary_uri:
			self.connection_pools['secondary'] = ConnectionPool(self.secondary_uri, connection_name='secondary')
			logger.info("Secondary connection pool configured")
		else:
			logger.warning("No secondary MongoDB URI provided")

		self.databases: Dict[str, AsyncIOMotorDatabase] = {}
		self.collections: Dict[str, CollectionManager] = {}
		self._collection_configs: Dict[str, CollectionConfig] = {}
		self._initialized = False
		self._lock = asyncio.Lock()

		# Define collection configurations with indexes
		self._define_collection_configs()

	def _define_collection_configs(self):
		"""Define collection configurations including indexes for optimal performance."""

		# Daily collections
		self._collection_configs['daily_wyr'] = CollectionConfig(
			name='WYR',
			database='Daily',
			connection='primary',
			indexes=[
				IndexModel([('date', -1)]),
				IndexModel([('guild_id', 1), ('date', -1)]),
				IndexModel([('created_at', -1)])
			]
		)

		self._collection_configs['daily_wyr_leaderboard'] = CollectionConfig(
			name='WYR_Leaderboard',
			database='Daily',
			connection='primary',
			indexes=[
				IndexModel([('user_id', 1), ('guild_id', 1)]),
				IndexModel([('score', -1)]),
				IndexModel([('updated_at', -1)])
			]
		)

		# Guide collections
		self._collection_configs['guide_menues'] = CollectionConfig(
			name='Menus',
			database='Guide',
			connection='primary',
				indexes=[
					IndexModel([('user_id', 1), ('guild_id', 1)]),
					IndexModel([('score', -1)]),
					IndexModel([('updated_at', -1)])
				]
			)

		# Prime Drops Collections
		self._collection_configs['prime_drops'] = CollectionConfig(
			name='AmazonPrime',
			database='PrimeDrops',
			connection='primary',
			indexes=[
				# Todo - Add indexes for AmazonPrime
			]
		)

		# Profile collections
		self._collection_configs['profilecard_themes'] = CollectionConfig(
			name='CustomThemes',
			database='ProfileCard',
			connection='primary',
			indexes=[
				#Todo - Add indexes for CustomThemes
			]
		)

		self._collection_configs['profilecard_preferences'] = CollectionConfig(
			name='ProfilePreferences',
			database='ProfileCard',
			connection='primary',
			indexes=[
				#Todo - Add indexes for ProfilePreferences
			]
		)

		# ServerData collections
		self._collection_configs['serverdata_guilds'] = CollectionConfig(
			name='Guilds',
			database='ServerData',
			connection='primary',
			indexes=[
				IndexModel([('id', 1)], unique=True),
				IndexModel([('owner_id', 1)]),
				IndexModel([('member_count', -1)]),
				IndexModel([('updated_at', -1)])
			]
		)

		self._collection_configs['serverdata_channels'] = CollectionConfig(
			name='Channels',
			database='ServerData',
			connection='primary',
			indexes=[
				IndexModel([('guild_id', 1), ('id', 1)], unique=True),
				IndexModel([('guild_id', 1), ('type', 1)]),
				IndexModel([('guild_id', 1), ('category_id', 1)])
			]
		)

		self._collection_configs['serverdata_members'] = CollectionConfig(
			name='Members',
			database='ServerData',
			connection='primary',
			indexes=[
				IndexModel([('guild_id', 1), ('id', 1)], unique=True),
				IndexModel([('guild_id', 1), ('bot', 1)]),
				IndexModel([('guild_id', 1), ('joined_at', -1)]),
				IndexModel([('guild_id', 1), ('roles', 1)])
			]
		)

		self._collection_configs['serverdata_users'] = CollectionConfig(
			name='Members',
			database='ServerData',
			connection='primary',
			indexes=[
				IndexModel([('id', 1)], unique=True),
				IndexModel([('username', 1)]),
				IndexModel([('created_at', -1)])
			]
		)

		self._collection_configs['serverdata_analytics'] = CollectionConfig(
			name='Analytics',
			database='ServerData',
			connection='primary',
			indexes=[
				IndexModel([('guild_id', 1), ('date', -1)]),
				IndexModel([('date', -1)]),
				IndexModel([('timestamp', -1)])
			]
		)

		self._collection_configs['serverdata_events'] = CollectionConfig(
			name='Events',
			database='ServerData',
			connection='primary',
			indexes=[
				IndexModel([('guild_id', 1), ('timestamp', -1)]),
				IndexModel([('event_type', 1), ('timestamp', -1)]),
				IndexModel([('timestamp', -1)])
			],
			capped=True,
			max_size=100 * 1024 * 1024,  # 100MB
			max_documents=1000000
		)

		# Suggestions collections
		self._collection_configs['suggestions_suggestions'] = CollectionConfig(
			name='Suggestions',
			database='Suggestions',
			connection='primary',
			indexes=[
				IndexModel([('guild_id', 1), ('status', 1)]),
				IndexModel([('author_id', 1)]),
				IndexModel([('created_at', -1)]),
				IndexModel([('guild_id', 1), ('suggestion_id', 1)], unique=True)
			]
		)

		self._collection_configs['suggestions_votes'] = CollectionConfig(
			name='Votes',
			database='Suggestions',
			connection='primary',
			indexes=[
				IndexModel([('suggestion_id', 1), ('user_id', 1)], unique=True),
				IndexModel([('suggestion_id', 1)]),
				IndexModel([('user_id', 1), ('created_at', -1)])
			]
		)

		self._collection_configs['suggestions_templates'] = CollectionConfig(
			name='Templates',
			database='Suggestions',
			connection='primary',
			indexes=[
				# Todo - Add indexes for Templates
			]
		)

		self._collection_configs['suggestions_userstats'] = CollectionConfig(
			name='UserStats',
			database='Suggestions',
			connection='primary',
			indexes=[
				# Todo - Add indexes
			]
		)

		self._collection_configs['suggestions_notification_queue'] = CollectionConfig(
			name='NotificationQueue',
			database='Suggestions',
			connection='primary',
			indexes=[
				# Todo - Add indexes
			]
		)

		# Updates and Drops collections
		self._collection_configs['updates_monthly'] = CollectionConfig(
			name='StatsMonthly',
			database='Updates-Drops',
			connection='primary',
			indexes=[
				# Todo - Add indexes for StatsMonthly
			]
		)

		self._collection_configs['updates_totals'] = CollectionConfig(
			name='StatsTotals',
			database='Updates-Drops',
			connection='primary',
			indexes=[
				# Todo - Add indexes for StatsTotals
			]
		)

		self._collection_configs['updates_users'] = CollectionConfig(
			name='Users',
			database='Ecom-Server',
			connection='secondary',
			indexes=[
			]
		)
		logger.info("Database Manager initialized\n\n\n\n\n\n\n")

	# Add more collection configurations as needed...

	async def initialize(self):
		"""Initialize the database manager with connection pooling and collection setup."""
		if self._initialized:
			return

		async with self._lock:
			if self._initialized:
				return

			try:
				logger.info("Initializing DatabaseManager...")

				# Initialize all connection pools
				for name, pool in self.connection_pools.items():
					await pool.initialize()
					logger.info(f"Initialized {name} connection pool")

				# Initialize databases using both connections as needed
				primary_client = await self.connection_pools['primary'].get_client()

				# Primary databases
				self.databases['Daily'] = primary_client['Daily']
				self.databases['Guide'] = primary_client['Guide']
				self.databases['PrimeDrops'] = primary_client['PrimeDrops']
				self.databases['ProfileCard'] = primary_client['ProfileCard']
				self.databases['Suggestions'] = primary_client['Suggestions']
				self.databases['ServerData'] = primary_client['ServerData']
				self.databases['Updates-Drops'] = primary_client['Updates-Drops']

				# Secondary databases (if a secondary connection exists)
				if 'secondary' in self.connection_pools:
					secondary_client = await self.connection_pools['secondary'].get_client()
					self.databases['Ecom-Server'] = secondary_client['Ecom-Server']
				else:
					# Fallback to primary if secondary not available
					self.databases['Ecom-Server'] = primary_client['Ecom-Server']

				# Initialize collections with managers
				await self._initialize_collections()

				# Create indexes
				await self._create_all_indexes()

				self._initialized = True
				logger.info("DatabaseManager initialized successfully")

			except Exception as e:
				logger.error(f"Failed to initialize DatabaseManager: {e}")
				raise

	async def _initialize_collections(self):
		"""Initialize collection managers."""
		for config_key, config in self._collection_configs.items():
			try:
				# Get the appropriate client based on the config's connection
				connection_name = config.connection
				if connection_name not in self.connection_pools:
					logger.warning(
						f"Connection '{connection_name}' not available for {config_key}, falling back to primary")
					connection_name = 'primary'

				client = await self.connection_pools[connection_name].get_client()
				database = client[config.database]
				collection = database[config.name]

				# Create capped collection if specified
				if config.capped:
					try:
						await database.create_collection(
							config.name,
							capped=True,
							size=config.max_size,
							max=config.max_documents
						)
					except Exception:
						# Collection might already exist
						pass

				manager = CollectionManager(collection, config)
				self.collections[config_key] = manager

				logger.debug(f"Initialized collection manager for {config_key} on {connection_name} connection")

			except Exception as e:
				logger.error(f"Error initializing collection {config_key}: {e}")
				raise

	async def _create_all_indexes(self):
		"""Create indexes for all collections."""
		for config_key, manager in self.collections.items():
			try:
				await manager.create_indexes()
			except Exception as e:
				logger.warning(f"Error creating indexes for {config_key}: {e}")

	def _ensure_initialized(self):
		"""Ensure the database manager is initialized."""
		if not self._initialized:
			raise RuntimeError("DatabaseManager not initialized. Call initialize() first.")

	# Collection Access Methods

	def get_database(self, name: str) -> AsyncIOMotorDatabase:
		"""
		Get a database by name.

		Args:
			name: Database name

		Returns:
			Database instance
		"""
		self._ensure_initialized()
		if name not in self.databases:
			client = self.connection_pools.get(name)
			self.databases[name] = client[name]
		return self.databases[name]

	def get_collection_manager(self, collection_key: str) -> CollectionManager:
		"""
		Get a collection manager by key.

		Args:
			collection_key: Collection configuration key

		Returns:
			CollectionManager instance
		"""
		self._ensure_initialized()
		if collection_key not in self.collections:
			raise ValueError(f"Collection '{collection_key}' not configured")
		return self.collections[collection_key]

	def get_raw_collection(self, database_name: str, collection_name: str) -> AsyncIOMotorCollection:
		"""
		Get raw collection access for advanced operations.

		Args:
			database_name: Database name
			collection_name: Collection name

		Returns:
			Raw collection instance
		"""
		database = self.get_database(database_name)
		return database[collection_name]

	def get_client(self, connection_name: str = 'primary') -> AsyncIOMotorClient:
		"""
		Get a client for a specific connection.

		Args:
			connection_name: Name of the connection pool ('primary' or 'secondary')

		Returns:
			AsyncIOMotorClient instance
		"""
		self._ensure_initialized()
		if connection_name not in self.connection_pools:
			raise ValueError(f"Connection '{connection_name}' not configured")
		return self.connection_pools[connection_name].client

	async def get_client_async(self, connection_name: str = 'primary') -> AsyncIOMotorClient:
		"""
		Get a client for a specific connection asynchronously.

		Args:
			connection_name: Name of the connection pool ('primary' or 'secondary')

		Returns:
			AsyncIOMotorClient instance
		"""
		self._ensure_initialized()
		if connection_name not in self.connection_pools:
			raise ValueError(f"Connection '{connection_name}' not configured")
		return await self.connection_pools[connection_name].get_client()

	def get_database_from_connection(self, database_name: str,
									 connection_name: str = 'primary') -> AsyncIOMotorDatabase:
		"""
		Get a database from a specific connection.

		Args:
			database_name: Database name
			connection_name: Connection pool name

		Returns:
			Database instance from specified connection
		"""
		self._ensure_initialized()
		if connection_name not in self.connection_pools:
			raise ValueError(f"Connection '{connection_name}' not configured")

		client = self.connection_pools[connection_name].client
		return client[database_name]

	def get_raw_collection_from_connection(self, database_name: str, collection_name: str,
										   connection_name: str = 'primary') -> AsyncIOMotorCollection:
		"""
		Get raw collection access from a specific connection for advanced operations.

		Args:
			database_name: Database name
			collection_name: Collection name
			connection_name: Connection pool name

		Returns:
			Raw collection instance from specified connection
		"""
		database = self.get_database_from_connection(database_name, connection_name)
		return database[collection_name]

	# Convenience Properties for Backward Compatibility

	@property
	def daily_wyr(self) -> CollectionManager:
		"""Get Daily WYR collection manager."""
		return self.get_collection_manager('daily_wyr')

	@property
	def daily_wyr_leaderboard(self) -> CollectionManager:
		"""Get Daily WYR Leaderboard collection manager."""
		return self.get_collection_manager('daily_wyr_leaderboard')

	@property
	def serverdata_guilds(self) -> CollectionManager:
		"""Get ServerData Guilds collection manager."""
		return self.get_collection_manager('serverdata_guilds')

	@property
	def serverdata_channels(self) -> CollectionManager:
		"""Get ServerData Channels collection manager."""
		return self.get_collection_manager('serverdata_channels')

	@property
	def serverdata_members(self) -> CollectionManager:
		"""Get ServerData Members collection manager."""
		return self.get_collection_manager('serverdata_members')

	@property
	def serverdata_users(self) -> CollectionManager:
		"""Get ServerData Users collection manager."""
		return self.get_collection_manager('serverdata_users')

	@property
	def serverdata_analytics(self) -> CollectionManager:
		"""Get ServerData Analytics collection manager."""
		return self.get_collection_manager('serverdata_analytics')

	@property
	def serverdata_events(self) -> CollectionManager:
		"""Get ServerData Events collection manager."""
		return self.get_collection_manager('serverdata_events')

	@property
	def suggestions_suggestions(self) -> CollectionManager:
		"""Get Suggestions collection manager."""
		return self.get_collection_manager('suggestions_suggestions')

	@property
	def suggestions_votes(self) -> CollectionManager:
		"""Get Suggestions Votes collection manager."""
		return self.get_collection_manager('suggestions_votes')

	# Transaction Support

	async def start_session(self, connection_name: str = 'primary', **kwargs):
		"""Start a new database session for transactions on specified connection."""
		self._ensure_initialized()
		client = await self.get_client_async(connection_name)
		return await client.start_session(**kwargs)

	async def with_transaction(self, callback: Callable, session_options: Dict = None,
							   connection_name: str = 'primary'):
		"""
		Execute a callback within a transaction on specified connection.

		Args:
			callback: Async function to execute within transaction
			session_options: Options for the session
			connection_name: Connection pool name

		Returns:
			Result of the callback function
		"""
		session_options = session_options or {}

		async with await self.start_session(connection_name, **session_options) as session:
			async with session.start_transaction():
				return await callback(session)

	# Utility Methods

	async def get_database_stats(self) -> Dict[str, Any]:
		"""Get comprehensive database statistics."""
		self._ensure_initialized()

		stats = {
			'databases': {},
			'total_collections': 0,
			'total_documents': 0,
			'total_size': 0
		}

		try:
			client = await self.connection_pools['primary'].get_client()

			for db_name, database in self.databases.items():
				db_stats = await database.command('dbStats')
				collection_stats = {}

				for collection_key, manager in self.collections.items():
					if manager.config.database == db_name:
						coll_stats = await manager.get_stats()
						collection_stats[manager.name] = coll_stats
						stats['total_documents'] += coll_stats.get('count', 0)
						stats['total_size'] += coll_stats.get('size', 0)

				stats['databases'][db_name] = {
					'collections': db_stats.get('collections', 0),
					'dataSize': db_stats.get('dataSize', 0),
					'indexSize': db_stats.get('indexSize', 0),
					'collection_details': collection_stats
				}

				stats['total_collections'] += db_stats.get('collections', 0)

			return stats

		except Exception as e:
			logger.error(f"Error getting database stats: {e}")
			return stats


	async def cleanup_old_data(self, days_to_keep: int = 90):
		"""
		Clean up old data across collections based on created_at field.

		Args:
			days_to_keep: Number of days of data to retain
		"""
		cutoff_date = datetime.now(tz=pytz.UTC) - timedelta(days=days_to_keep)
		cleanup_results = {}

		# Collections that should have old data cleaned up
		cleanup_collections = [
			'serverdata_analytics',
			'serverdata_events'
		]

		for collection_key in cleanup_collections:
			if collection_key in self.collections:
				try:
					manager = self.collections[collection_key]
					deleted_count = await manager.delete_many({
						'created_at': {'$lt': cutoff_date}
					})
					cleanup_results[collection_key] = deleted_count
					logger.info(f"Cleaned up {deleted_count} old records from {collection_key}")
				except Exception as e:
					logger.error(f"Error cleaning up {collection_key}: {e}")
					cleanup_results[collection_key] = f"Error: {e}"

		return cleanup_results

	async def health_check(self) -> Dict[str, Any]:
		"""Perform comprehensive health check on all connections."""
		health_status = {
			'status': 'healthy',
			'timestamp': datetime.now(tz=pytz.UTC).isoformat(),
			'connections': {},
			'databases': {},
			'collections': {}
		}

		try:
			# Check all connection pools
			for name, pool in self.connection_pools.items():
				try:
					client = await pool.get_client()
					await client.admin.command('ping')
					health_status['connections'][name] = 'healthy'
				except Exception as e:
					health_status['connections'][name] = f'error: {e}'
					health_status['status'] = 'degraded'

			# Check each database (using primary connection)
			for db_name in self.databases.keys():
				try:
					db = self.get_database(db_name)
					await db.command('ping')
					health_status['databases'][db_name] = 'healthy'
				except Exception as e:
					health_status['databases'][db_name] = f'error: {e}'
					health_status['status'] = 'degraded'

			# Check collection managers
			for collection_key, manager in self.collections.items():
				try:
					await manager.count_documents({})
					health_status['collections'][collection_key] = 'healthy'
				except Exception as e:
					health_status['collections'][collection_key] = f'error: {e}'
					health_status['status'] = 'degraded'

		except Exception as e:
			health_status['status'] = 'unhealthy'
			health_status['error'] = str(e)

		return health_status

	async def close(self):
		"""Close all database connections and cleanup resources."""
		try:
			logger.info("Closing DatabaseManager...")

			# Clear collections and databases
			self.collections.clear()
			self.databases.clear()

			# Close all connection pools
			for name, pool in self.connection_pools.items():
				await pool.close()
				logger.info(f"Closed {name} connection pool")

			self.connection_pools.clear()
			self._initialized = False
			logger.info("DatabaseManager closed successfully")

		except Exception as e:
			logger.error(f"Error closing DatabaseManager: {e}")



# Global database manager instance
db_manager = DatabaseManager()


# Utility functions for common database patterns

async def ensure_unique_constraint(manager: CollectionManager,
								   field: str,
								   value: Any,
								   exclude_id: Any = None) -> bool:
	"""
	Ensure a field value is unique in the collection.

	Args:
		manager: Collection manager
		field: Field name to check
		value: Value that should be unique
		exclude_id: Document ID to exclude from uniqueness check

	Returns:
		True if value is unique, False otherwise
	"""
	filter_dict = {field: value}
	if exclude_id:
		filter_dict['_id'] = {'$ne': exclude_id}

	existing = await manager.find_one(filter_dict)
	return existing is None


async def paginate_results(manager: CollectionManager,
						   filter_dict: Dict[str, Any] = None,
						   sort: List[tuple] = None,
						   page_size: int = 50,
						   page: int = 1) -> Dict[str, Any]:
	"""
	Paginate query results.

	Args:
		manager: Collection manager
		filter_dict: Query filter
		sort: Sort specification
		page_size: Number of items per page
		page: Page number (1-based)

	Returns:
		Dictionary with pagination info and results
	"""
	filter_dict = filter_dict or {}
	skip = (page - 1) * page_size

	# Get total count and results concurrently
	total_count, results = await asyncio.gather(
		manager.count_documents(filter_dict),
		manager.find_many(filter_dict, sort=sort, limit=page_size, skip=skip)
	)

	total_pages = (total_count + page_size - 1) // page_size

	return {
		'results': results,
		'pagination': {
			'current_page': page,
			'page_size': page_size,
			'total_items': total_count,
			'total_pages': total_pages,
			'has_next': page < total_pages,
			'has_prev': page > 1
		}
	}


async def batch_upsert(manager: CollectionManager,
					   documents: List[Dict[str, Any]],
					   match_fields: List[str]) -> Dict[str, int]:
	"""
	Perform batch upsert operations based on matching fields.

	Args:
		manager: Collection manager
		documents: List of documents to upsert
		match_fields: Fields to match for upsert decision

	Returns:
		Dictionary with counts of inserted and updated documents
	"""
	if not documents or not match_fields:
		return {'inserted': 0, 'updated': 0}

	operations = []
	now = datetime.now(tz=pytz.UTC)

	for doc in documents:
		# Build filter from match fields
		filter_dict = {field: doc[field] for field in match_fields if field in doc}

		# Prepare update document
		update_doc = doc.copy()
		update_doc['updated_at'] = now
		if 'created_at' not in update_doc:
			update_doc['created_at'] = now

		operation = UpdateOne(
			filter_dict,
			{'$set': update_doc},
			upsert=True
		)
		operations.append(operation)

	result = await manager.bulk_write(operations, ordered=False)

	return {
		'inserted': result['inserted_count'] + result['upserted_count'],
		'updated': result['modified_count']
	}