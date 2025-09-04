import asyncio
import os
import re
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timedelta
import discord
from discord.ui import view
from motor.motor_asyncio import AsyncIOMotorClient
from fuzzywuzzy import fuzz, process
from utils.bot import bot
from utils.logger import get_logger, PerformanceLogger
from Database.DatabaseManager import db_manager


logger = get_logger("Guide")


class SearchEngine:
	"""Advanced search engine for content matching"""

	def __init__(self):
		logger.debug("Initializing SearchEngine")
		self.content_index = {}
		self.user_interactions = {}
		self.popular_paths = {}
		logger.info("SearchEngine initialized successfully")

	def index_content(self, content_data: Dict):
		"""Index all content for fast searching"""
		logger.info(
			f"Starting content indexing with {len(content_data) if isinstance(content_data, (list, dict)) else 'unknown'} items")

		with PerformanceLogger(logger, "index_content"):
			def index_recursive(items, path=""):
				if not items:
					logger.debug(f"No items to index at path: {path}")
					return

				# Handle both list and single item cases
				if isinstance(items, dict):
					items = [items]
				elif not isinstance(items, list):
					logger.warning(f"Unexpected data type for items: {type(items)} at path: {path}")
					return

				for item in items:
					if not isinstance(item, dict):
						logger.warning(f"Skipping non-dict item: {item} at path: {path}")
						continue

					name = item.get('name', '')
					if not name:
						logger.warning(f"Skipping item without name: {item} at path: {path}")
						continue

					description = item.get('description', '')
					meta_description = item.get('meta_description', '')

					# Handle description as list or string
					if isinstance(description, list):
						description = ' '.join(description)

					# Create searchable text
					searchable_text = f"{name} {description} {meta_description}".lower()

					keywords = self._extract_keywords(searchable_text)
					self.content_index[name] = {
						'text': searchable_text,
						'path': path,
						'item': item,
						'keywords': keywords
					}

					logger.debug(f"Indexed: {name} at path: {path} with {len(keywords)} keywords")

					# Index nested items
					if 'options' in item and item['options']:
						logger.debug(f"Processing {len(item['options'])} nested options for: {name}")
						index_recursive(item['options'], f"{path}/{name}")

			# Clear existing index
			old_count = len(self.content_index)
			self.content_index = {}
			logger.debug(f"Cleared existing index with {old_count} items")

			try:
				index_recursive(content_data)
				logger.info(f"Successfully indexed {len(self.content_index)} items")
			except Exception as e:
				logger.error(f"Error during indexing: {e}")
				import traceback
				logger.error(traceback.format_exc())
				raise

	def _extract_keywords(self, text: str) -> List[str]:
		"""Extract important keywords from text"""
		logger.debug(f"Extracting keywords from text of length: {len(text)}")

		# Remove common words and extract meaningful terms
		words = re.findall(r'\b\w{2,}\b', text.lower())
		stop_words = {
			'the', 'and', 'for', 'are', 'but', 'not', 'you', 'all', 'can',
			'how', 'has', 'had', 'with', 'this', 'that', 'will', 'from',
			'they', 'been', 'have', 'were', 'said', 'each', 'which',
			'she', 'him', 'her', 'his', 'their', 'what', 'when', 'where'
		}
		keywords = [word for word in words if word not in stop_words and len(word) >= 2]

		# Also include original words without filtering for exact matches
		all_words = re.findall(r'\b\w+\b', text.lower())
		keywords.extend(all_words)

		unique_keywords = list(set(keywords))
		logger.debug(f"Extracted {len(unique_keywords)} unique keywords from {len(words)} total words")
		return unique_keywords

	def smart_search(self, query: str, limit: int = 5) -> List[Tuple[str, int, str]]:
		"""Perform intelligent content search"""
		logger.info(f"Starting smart search for query: '{query}' (limit: {limit})")

		with PerformanceLogger(logger, f"smart_search_'{query[:20]}'"):
			query_lower = query.lower()
			results = []

			logger.debug(f"Searching for: '{query}' in {len(self.content_index)} indexed items")

			if not self.content_index:
				logger.warning("Search index is empty! Cannot perform search")
				return []

			matches_found = 0
			for name, data in self.content_index.items():
				score = 0
				name_lower = name.lower()

				# Exact name match (highest priority)
				if query_lower == name_lower:
					score += 200
					logger.debug(f"Exact match found: {name}")
				elif query_lower in name_lower:
					score += 150
					logger.debug(f"Substring match found: {name}")

				# Multi-word exact matches get higher priority
				query_words = query_lower.split()
				name_words = name_lower.split()

				# Count exact word matches
				exact_word_matches = 0
				for q_word in query_words:
					if q_word in name_words:
						exact_word_matches += 1
						score += 40

				# Bonus for matching multiple words
				if len(query_words) > 1 and exact_word_matches > 1:
					match_ratio = exact_word_matches / len(query_words)
					score += int(100 * match_ratio)

				# Partial name match (word contains)
				for q_word in query_words:
					for n_word in name_words:
						if q_word in n_word and q_word != n_word:
							score += 25
						elif n_word in q_word and q_word != n_word:
							score += 25

				# Keyword matches
				query_keywords = self._extract_keywords(query_lower)
				keyword_matches = 0
				for keyword in query_keywords:
					if keyword in data['keywords']:
						keyword_matches += 1
						score += 30

				# Bonus for multiple keyword matches
				if keyword_matches > 1:
					score += keyword_matches * 10

				# Fuzzy matching on full text
				fuzzy_score = fuzz.partial_ratio(query_lower, data['text'])
				score += fuzzy_score // 2

				# Special scoring for semantic matches
				semantic_bonus = self._calculate_semantic_bonus(query_lower, name_lower, data['text'])
				score += semantic_bonus

				# Context-based scoring (if user has accessed similar content)
				if hasattr(self, 'user_context'):
					context_bonus = self._get_context_score(data['path'])
					score += context_bonus

				# Lower the minimum threshold but prioritize better matches
				if score > 15:
					results.append((name, score, data['path']))
					matches_found += 1
					logger.debug(
						f"Match found: {name} (score: {score}) - exact_words: {exact_word_matches}, keywords: {keyword_matches}")

			# Sort by score and return top results
			results.sort(key=lambda x: x[1], reverse=True)
			logger.info(f"Found {len(results)} results for query: '{query}' (processed {matches_found} matches)")

			# Log top 5 results for debugging
			for i, (name, score, path) in enumerate(results[:5], 1):
				logger.debug(f"  {i}. {name} (score: {score}) at path: {path}")

			return results[:limit]

	def _calculate_semantic_bonus(self, query: str, name: str, text: str) -> int:
		"""Calculate semantic bonus based on context and meaning"""
		logger.debug(f"Calculating semantic bonus for query: '{query[:20]}...' and name: '{name[:20]}...'")

		bonus = 0

		# Define semantic groups
		semantic_groups = {
			'music': ['music', 'song', 'audio', 'sound', 'tune', 'melody', 'beat'],
			'bot': ['bot', 'command', 'automation', 'assistant'],
			'game': ['game', 'play', 'gaming', 'player', 'match', 'competition'],
			'server': ['server', 'discord', 'guild', 'community'],
			'help': ['help', 'guide', 'tutorial', 'instruction', 'manual'],
			'search': ['search', 'find', 'lookup', 'query']
		}

		query_words = set(query.split())
		name_words = set(name.split())
		text_words = set(text.split())

		# Check for semantic group matches
		for group, keywords in semantic_groups.items():
			query_has_group = any(word in keywords for word in query_words)
			name_has_group = any(word in keywords for word in name_words)

			if query_has_group and name_has_group:
				bonus += 50
				logger.debug(f"Semantic match found for group '{group}': +50 points")

		# Special cases for better matching
		if 'music' in query and 'bot' in query:
			if 'music' in name and ('bot' in name or 'command' in text):
				bonus += 100
				logger.debug("Strong music bot context match: +100 points")

		if 'command' in query:
			if 'command' in name or 'command' in text:
				bonus += 60
				logger.debug("Command context match: +60 points")

		# Penalty for mismatched contexts
		if 'music' in query and 'server' in name and 'search' in name:
			bonus -= 30
			logger.debug("Context mismatch penalty: -30 points")

		logger.debug(f"Total semantic bonus: {bonus}")
		return bonus

	def _get_context_score(self, path: str) -> int:
		"""Get contextual scoring bonus based on user behavior"""
		# This could be enhanced with user interaction tracking
		logger.debug(f"Getting context score for path: {path}")
		return 0

	def suggest_alternatives(self, failed_query: str) -> List[str]:
		"""Suggest alternative searches when no results found"""
		logger.info(f"Suggesting alternatives for failed query: '{failed_query}'")

		suggestions = []

		# Try partial matches
		for name in self.content_index.keys():
			if fuzz.partial_ratio(failed_query.lower(), name.lower()) > 60:
				suggestions.append(name)

		logger.debug(f"Found {len(suggestions)} partial match suggestions")

		# Try related keywords
		query_words = failed_query.lower().split()
		for word in query_words:
			for name, data in self.content_index.items():
				if word in data['text'] and name not in suggestions:
					suggestions.append(name)

		logger.info(f"Generated {len(suggestions)} total suggestions, returning top 3")
		return suggestions[:3]


class NavigationBreadcrumbs:
	"""Handle breadcrumb navigation for better user orientation"""

	def __init__(self):
		logger.debug("Initializing NavigationBreadcrumbs")
		self.breadcrumbs = {}
		logger.info("NavigationBreadcrumbs initialized successfully")

	def update_breadcrumb(self, user_id: int, path: List[str]):
		"""Update user's navigation breadcrumb"""
		logger.debug(f"Updating breadcrumb for user {user_id} with path: {' -> '.join(path)}")
		self.breadcrumbs[user_id] = {
			'path': path,
			'timestamp': datetime.now()
		}
		logger.info(f"Breadcrumb updated for user {user_id}: {len(path)} levels deep")

	def get_breadcrumb_display(self, user_id: int) -> str:
		"""Get formatted breadcrumb display"""
		logger.debug(f"Getting breadcrumb display for user {user_id}")

		if user_id not in self.breadcrumbs:
			logger.debug(f"No breadcrumb found for user {user_id}")
			return ""

		path = self.breadcrumbs[user_id]['path']
		if not path:
			logger.debug(f"Empty breadcrumb path for user {user_id}")
			return ""

		display = " → ".join(path[-3:])  # Show last 3 levels
		logger.debug(f"Generated breadcrumb display for user {user_id}: '{display}'")
		return display

	def get_navigation_path(self, user_id: int) -> List[str]:
		"""Get full navigation path for user"""
		logger.debug(f"Getting full navigation path for user {user_id}")

		if user_id not in self.breadcrumbs:
			logger.debug(f"No navigation path found for user {user_id}")
			return []

		path = self.breadcrumbs[user_id]['path']
		logger.debug(f"Retrieved navigation path for user {user_id}: {len(path)} items")
		return path


class QuickAccessManager:
	"""Manage frequently accessed content and shortcuts"""

	def __init__(self):
		logger.debug("Initializing QuickAccessManager")
		self.access_count = {}
		self.user_favorites = {}
		self.trending_content = {}
		logger.info("QuickAccessManager initialized successfully")

	def track_access(self, user_id: int, content_name: str):
		"""Track content access for analytics"""
		key = f"{user_id}:{content_name}"
		old_count = self.access_count.get(key, 0)
		self.access_count[key] = old_count + 1

		# Update trending
		old_trending = self.trending_content.get(content_name, 0)
		self.trending_content[content_name] = old_trending + 1

		logger.debug(f"Tracked access for user {user_id}, content '{content_name}': {old_count} -> {old_count + 1}")
		logger.info(f"Content access tracked: user={user_id}, content='{content_name}', total_accesses={old_count + 1}")

	def get_user_shortcuts(self, user_id: int) -> List[str]:
		"""Get user's most accessed content as shortcuts"""
		logger.debug(f"Getting shortcuts for user {user_id}")

		user_content = {}
		for key, count in self.access_count.items():
			if key.startswith(f"{user_id}:"):
				content = key.split(":", 1)[1]
				user_content[content] = count

		# Return top 5 most accessed
		sorted_content = sorted(user_content.items(), key=lambda x: x[1], reverse=True)
		shortcuts = [content for content, _ in sorted_content[:5]]

		logger.info(f"Generated {len(shortcuts)} shortcuts for user {user_id}")
		return shortcuts

	def get_trending_content(self, limit: int = 3) -> List[str]:
		"""Get trending content across all users"""
		logger.debug(f"Getting trending content (limit: {limit})")

		sorted_trending = sorted(self.trending_content.items(), key=lambda x: x[1], reverse=True)
		trending = [content for content, _ in sorted_trending[:limit]]

		logger.info(f"Retrieved {len(trending)} trending items from {len(self.trending_content)} total items")
		return trending


class GuideManager:
	"""Enhanced Guide Manager with improved navigation and search"""

	def __init__(self):
		logger.info("Initializing GuideManager")
		self.search_engine = SearchEngine()
		self.navigation = NavigationBreadcrumbs()
		self.quick_access = QuickAccessManager()
		self.content_cache = {}
		self.cache_timestamp = None
		logger.info("GuideManager initialized successfully")

	async def initialize_database(self):
		"""Initialize the database connection using DatabaseManager"""
		logger.info("Initializing database connection using DatabaseManager")

		try:
			with PerformanceLogger(logger, "database_initialization"):
				# Initialize the global database manager
				await db_manager.initialize()

				logger.debug("DatabaseManager initialized, building search index")
				# Build search index
				await self._build_search_index()

				logger.info("✅ GuideManager: Database initialized successfully with search index")
				return True
		except Exception as e:
			logger.error(f"❌ GuideManager: Failed to initialize database - {e}")
			logger.exception("Database initialization error details:")
			return False

	async def _build_search_index(self):
		"""Build search index from database content using DatabaseManager"""
		logger.info("Building search index from database content")

		try:
			with PerformanceLogger(logger, "build_search_index"):
				# Use DatabaseManager to get the guide menus collection
				guide_collection = db_manager.get_collection_manager('guide_menues')
				data = await guide_collection.find_many({}, sort=[('order', 1)])

				logger.info(f"Retrieved {len(data)} documents from database")

				if data:
					logger.debug(f"Sample document structure: {list(data[0].keys()) if data else 'No data'}")

				# Build content cache with ALL items (including nested ones)
				old_cache_size = len(self.content_cache)
				self.content_cache = {}

				def cache_recursive(items, parent_path=""):
					"""Recursively cache all items including nested ones"""
					if not items:
						logger.debug(f"No items to cache at path: {parent_path}")
						return

					if isinstance(items, dict):
						items = [items]
					elif not isinstance(items, list):
						logger.warning(f"Unexpected data type for caching: {type(items)} at path: {parent_path}")
						return

					cached_count = 0
					for item in items:
						if not isinstance(item, dict):
							logger.warning(f"Skipping non-dict item for caching at path: {parent_path}")
							continue

						name = item.get('name', '')
						if not name:
							logger.warning(f"Skipping item without name for caching at path: {parent_path}")
							continue

						# Cache this item
						self.content_cache[name] = item
						cached_count += 1

						# Recursively cache nested items
						if 'options' in item and item['options']:
							logger.debug(f"Caching {len(item['options'])} nested items for: {name}")
							cache_recursive(item['options'], f"{parent_path}/{name}")

					logger.debug(f"Cached {cached_count} items at path: {parent_path}")

				# Cache top-level items first
				top_level_cached = 0
				for item in data:
					if item.get('name'):
						self.content_cache[item['name']] = item
						top_level_cached += 1

				logger.info(f"Cached {top_level_cached} top-level items")

				# Then cache all nested items
				cache_recursive(data)

				# Now build the search index
				logger.debug("Building search engine index from cached content")
				self.search_engine.index_content(data)
				self.cache_timestamp = datetime.now()

				logger.info(f"Search index built successfully:")
				logger.info(f"  - Content cache: {len(self.content_cache)} items (was {old_cache_size})")
				logger.info(f"  - Search index: {len(self.search_engine.content_index)} items")
				logger.info(f"  - Cache timestamp: {self.cache_timestamp}")

				if self.content_cache:
					sample_keys = list(self.content_cache.keys())[:10]
					logger.debug(f"Sample cache keys: {sample_keys}")

		except Exception as e:
			logger.error(f"Failed to build search index: {e}")
			logger.exception("Search index build error details:")
			raise

	async def search_content(self, query: str, user_id: int = None) -> List[Dict]:
		"""Search for content using the enhanced search engine"""
		logger.info(f"Searching content for query: '{query}' (user: {user_id})")

		# Check if index is built
		if not self.search_engine.content_index:
			logger.warning("Search index is empty, rebuilding...")
			await self._build_search_index()

		with PerformanceLogger(logger, f"search_content_'{query[:20]}'"):
			results = self.search_engine.smart_search(query)
			search_results = []

			logger.debug(f"Smart search returned {len(results)} raw results")

			for name, score, path in results:
				if name in self.content_cache:
					item = self.content_cache[name]
					search_results.append({
						'name': name,
						'score': score,
						'path': path,
						'description': item.get('meta_description',
												item.get('description', 'No description available')),
						'type': item.get('type', 'unknown')
					})
					logger.debug(f"Added search result: {name} (score: {score})")
				else:
					logger.warning(f"Item '{name}' found in search index but not in cache")

			logger.info(f"Returning {len(search_results)} processed search results for query: '{query}'")
			return search_results

	async def debug_search_index(self):
		"""Debug method to check search index status"""
		logger.info("=== SEARCH INDEX DEBUG ===")
		logger.info(f"DatabaseManager initialized: {db_manager._initialized}")
		logger.info(f"Content cache size: {len(self.content_cache)}")
		logger.info(f"Search index size: {len(self.search_engine.content_index)}")
		logger.info(f"Cache timestamp: {self.cache_timestamp}")

		if self.content_cache:
			sample_keys = list(self.content_cache.keys())[:5]
			logger.info(f"Sample cache keys: {sample_keys}")

		if self.search_engine.content_index:
			sample_key = list(self.search_engine.content_index.keys())[0]
			sample_data = self.search_engine.content_index[sample_key]
			logger.info(f"Sample index entry: {sample_key}")
			logger.info(f"Sample keywords: {sample_data['keywords'][:10]}")
			logger.info(f"Sample text: {sample_data['text'][:100]}...")

		# Test a simple search
		test_results = self.search_engine.smart_search("help")
		logger.info(f"Test search for 'help' returned: {len(test_results)} results")
		logger.info("=== END DEBUG ===")

	async def get_help_menu(self, author_id=None, show_shortcuts=True):
		"""Enhanced help menu with shortcuts and trending content"""
		logger.info(f"Generating help menu for user {author_id} (shortcuts: {show_shortcuts})")

		try:
			with PerformanceLogger(logger, "get_help_menu"):
				# Refresh cache if needed
				cache_age = None
				if self.cache_timestamp:
					cache_age = datetime.now() - self.cache_timestamp
					logger.debug(f"Cache age: {cache_age.total_seconds():.2f} seconds")

				if (not self.cache_timestamp or
						datetime.now() - self.cache_timestamp > timedelta(minutes=30)):
					logger.info("Cache refresh needed, rebuilding search index")
					await self._build_search_index()

				# Use DatabaseManager to get the guide menus collection
				guide_collection = db_manager.get_collection_manager('guide_menues')
				data = await guide_collection.find_many({}, sort=[('order', 1)])
				options = [{"name": entry["name"], "meta_description": entry["meta_description"]} for entry in data]

				logger.debug(f"Retrieved {len(data)} categories, created {len(options)} menu options")

				# Create enhanced embed with additional features
				embed = discord.Embed(
					title="🏛️ Imperial Codex - Help Menu",
					description="Choose from the options below or use the enhanced features:",
					color=0x4D0EB3
				)

				# Add quick access section if user has history
				if author_id and show_shortcuts:
					shortcuts = self.quick_access.get_user_shortcuts(author_id)
					if shortcuts:
						shortcut_text = "\n".join([f"⚡ {shortcut}" for shortcut in shortcuts[:3]])
						embed.add_field(name="🚀 Your Quick Access", value=shortcut_text, inline=False)
						logger.debug(f"Added {len(shortcuts)} shortcuts to help menu for user {author_id}")

					# Add trending content
					trending = self.quick_access.get_trending_content()
					if trending:
						trending_text = "\n".join([f"📈 {item}" for item in trending])
						embed.add_field(name="📊 Trending", value=trending_text, inline=False)
						logger.debug(f"Added {len(trending)} trending items to help menu")

				# Add navigation help
				embed.add_field(
					name="💡 Pro Tips",
					value="• Use the search button for specific topics\n• Check your breadcrumbs for navigation\n• Quick access saves your frequent selections",
					inline=False
				)

				# Enhanced view with more features
				view = EnhancedHistoryTrackingView(
					options=options,
					author_id=author_id,
					search_manager=self.search_engine,
					quick_access_manager=self.quick_access
				)

				# Update navigation
				if author_id:
					self.navigation.update_breadcrumb(author_id, ["Main Menu"])

				logger.info(f"Successfully generated help menu for user {author_id}")
				return embed, view

		except Exception as e:
			logger.error(f"Failed to generate help menu: {e}")
			logger.exception("Help menu generation error details:")
			embed = discord.Embed(
				title="Error",
				description="There was an issue generating the help menu.",
				color=0xFF0000
			)
			view = discord.ui.View()
			return embed, view

	async def get_embed_for_selection(self, option_name, author_id=None, search_context=None):
		"""Enhanced selection handling with better navigation"""
		logger.info(f"Processing selection: '{option_name}' for user {author_id} (search_context: {search_context})")

		try:
			with PerformanceLogger(logger, f"get_embed_for_selection_'{option_name[:20]}'"):
				# Track access
				if author_id:
					self.quick_access.track_access(author_id, option_name)

				logger.debug(f"Fetching entry for selected option: {option_name}")

				# Use DatabaseManager to find the entry
				guide_collection = db_manager.get_collection_manager('guide_menues')
				entry = await guide_collection.find_one({"name": option_name})

				if entry:
					logger.info(f"Found top-level entry for '{option_name}' (type: {entry.get('type', 'unknown')})")

					# Update breadcrumb
					if author_id:
						current_path = self.navigation.get_navigation_path(author_id)
						if option_name not in current_path:
							current_path.append(option_name)
						self.navigation.update_breadcrumb(author_id, current_path)

					if entry["type"] == "select" and "options" in entry:
						embed = self._create_enhanced_embed(entry, author_id, option_name)
						view = EnhancedHistoryTrackingView(
							entry["options"],
							current_option=option_name,
							author_id=author_id,
							search_manager=self.search_engine,
							quick_access_manager=self.quick_access
						)
						logger.debug(f"Created select menu with {len(entry['options'])} options")
						return embed, view

					elif entry["type"] == "embed":
						embed = self._create_enhanced_embed(entry, author_id, option_name)
						view = EnhancedHistoryTrackingView(
							[],
							current_option=option_name,
							author_id=author_id,
							search_manager=self.search_engine,
							quick_access_manager=self.quick_access
						)
						logger.debug("Created simple embed view")
						return embed, view

				# Search nested options with enhanced context
				logger.info(f"No top-level match found. Checking sub-options for '{option_name}'")

				# Use DatabaseManager for nested search
				documents = await guide_collection.find_many({}, projection={"options": 1, "name": 1})
				documents_checked = 0

				for document in documents:
					documents_checked += 1
					result = await self.search_nested_options(
						document.get("options", []),
						option_name,
						author_id,
						parent_name=document.get("name", "")
					)
					if result:
						logger.info(
							f"Nested match found for '{option_name}' in document '{document.get('name', 'unnamed')}' (checked {documents_checked} documents)")
						return result

				logger.warning(f"No match found for '{option_name}' after checking {documents_checked} documents")

				# No match found - provide suggestions
				suggestions = self.search_engine.suggest_alternatives(option_name)
				embed = discord.Embed(
					title="🔍 Content Not Found",
					description=f"Couldn't find '{option_name}', but here are some suggestions:",
					color=0xFFA500
				)

				if suggestions:
					suggestion_text = "\n".join([f"• {suggestion}" for suggestion in suggestions])
					embed.add_field(name="Did you mean:", value=suggestion_text, inline=False)
					logger.info(f"Provided {len(suggestions)} suggestions for '{option_name}'")

				embed.add_field(
					name="💡 Tip",
					value="Try using the search button or browse categories from the main menu",
					inline=False
				)

				view = EnhancedHistoryTrackingView(
					[],
					author_id=author_id,
					search_manager=self.search_engine,
					quick_access_manager=self.quick_access
				)
				return embed, view

		except Exception as e:
			logger.error(f"An error occurred while processing option '{option_name}': {e}")
			logger.exception("Selection processing error details:")
			embed = discord.Embed(
				title="Error",
				description="An unexpected error occurred while fetching the help content.",
				color=0xFF0000
			)
			view = discord.ui.View()
			return embed, view

	def _create_enhanced_embed(self, entry, author_id, option_name):
		"""Create enhanced embed with navigation context"""
		logger.debug(f"Creating enhanced embed for '{option_name}' (user: {author_id})")

		if isinstance(entry.get("description"), list):
			description_str = "\n".join(entry["description"])
		else:
			description_str = entry.get("description", entry.get("meta_description", "No description available."))

		embed = discord.Embed(
			title=entry["name"],
			description=description_str,
			color=0x4D0EB3
		)

		# Add breadcrumb navigation
		if author_id:
			breadcrumb = self.navigation.get_breadcrumb_display(author_id)
			if breadcrumb:
				embed.set_footer(text=f"📍 {breadcrumb}")
				logger.debug(f"Added breadcrumb to embed: {breadcrumb}")

		# Add common fields
		if "channel_id" in entry and entry.get("channel_id") is not None:
			embed.add_field(name="📍 Channel", value=f"<#{entry['channel_id']}>", inline=False)
			logger.debug(f"Added channel reference: {entry['channel_id']}")

		if entry.get("footer"):
			embed.set_image(url=entry["footer"])
			logger.debug("Added footer image to embed")

		if entry.get("thumbnail"):
			embed.set_thumbnail(url=entry["thumbnail"])
			logger.debug("Added thumbnail to embed")

		logger.debug(f"Enhanced embed created successfully for '{option_name}'")
		return embed

	async def search_nested_options(self, options, option_name, author_id=None, parent_name=""):
		"""Enhanced nested search with parent context"""
		logger.debug(f"Searching nested options for '{option_name}' in parent '{parent_name}' ({len(options)} options)")

		for i, option in enumerate(options):
			logger.debug(f"Checking nested option {i + 1}/{len(options)}: {option.get('name', 'unnamed')}")

			if option.get("name") == option_name:
				logger.info(f"Match found for option: {option_name} in parent: {parent_name}")

				# Update breadcrumb with parent context
				if author_id and parent_name:
					current_path = self.navigation.get_navigation_path(author_id)
					if parent_name not in current_path:
						current_path.append(parent_name)
					if option_name not in current_path:
						current_path.append(option_name)
					self.navigation.update_breadcrumb(author_id, current_path)

				option_type = option.get("type")
				embed = self._create_enhanced_embed(option, author_id, option_name)

				if option_type == "select":
					view = EnhancedHistoryTrackingView(
						option.get("options", []),
						current_option=option_name,
						author_id=author_id,
						search_manager=self.search_engine,
						quick_access_manager=self.quick_access
					)
					logger.debug(f"Created nested select view with {len(option.get('options', []))} options")
				else:
					view = EnhancedHistoryTrackingView(
						[],
						current_option=option_name,
						author_id=author_id,
						search_manager=self.search_engine,
						quick_access_manager=self.quick_access
					)
					logger.debug("Created nested embed view")

				return embed, view

			# Recurse into deeper nested options
			if isinstance(option.get("options"), list):
				logger.debug(
					f"Recursing into {len(option['options'])} deeper nested options for {option.get('name', 'unnamed')}")
				result = await self.search_nested_options(
					option["options"],
					option_name,
					author_id,
					parent_name=option.get("name", parent_name)
				)
				if result:
					logger.debug(f"Found match in deeper nested options for {option.get('name', 'unnamed')}")
					return result

		logger.debug(f"No match found in {len(options)} nested options for '{option_name}'")
		return None

	async def intelligent_question_matching(self, question: str, author_id: int = None) -> Optional[Tuple[str, int]]:
		"""Advanced question matching using NLP-like techniques"""
		logger.info(f"Performing intelligent question matching for: '{question}' (user: {author_id})")

		# Clean the question
		question_clean = re.sub(r'[^\w\s]', '', question.lower())
		question_words = question_clean.split()
		logger.debug(f"Cleaned question: '{question_clean}' ({len(question_words)} words)")

		# Remove common question words
		question_starters = {'how', 'what', 'where', 'when', 'why', 'who', 'can', 'do', 'does', 'is', 'are'}
		meaningful_words = [word for word in question_words if word not in question_starters and len(word) > 2]
		logger.debug(f"Meaningful words extracted: {meaningful_words}")

		if not meaningful_words:
			logger.warning("No meaningful words found in question")
			return None

		# Search using meaningful words
		search_query = ' '.join(meaningful_words)
		logger.debug(f"Generated search query from meaningful words: '{search_query}'")

		results = await self.search_content(search_query, author_id)

		if results:
			# Return the best match
			best_match = results[0]
			logger.info(f"Best match found: '{best_match['name']}' with score {best_match['score']}")
			return best_match['name'], best_match['score']

		logger.info("No matches found for intelligent question matching")
		return None


# Create a global instance of the GuideManager
logger.info("Creating global GuideManager instance")
guide_manager = GuideManager()


class SearchModal(discord.ui.Modal):
	"""Modal for advanced content search"""

	def __init__(self, search_manager, author_id, guide_manager_ref):
		super().__init__(title="🔍 Search Imperial Codex")
		logger.debug(f"Initializing SearchModal for user {author_id}")
		self.search_manager = search_manager
		self.author_id = author_id
		self.guide_manager_ref = guide_manager_ref

		self.search_input = discord.ui.TextInput(
			label="What are you looking for?",
			placeholder="e.g., 'pokemon commands', 'music bot', 'server rules'...",
			max_length=100,
			required=True
		)
		self.add_item(self.search_input)

	async def on_submit(self, interaction: discord.Interaction):
		query = self.search_input.value
		logger.info(f"Search modal submitted by user {self.author_id} with query: '{query}'")

		try:
			# First try intelligent question matching
			match_result = await self.guide_manager_ref.intelligent_question_matching(query, self.author_id)

			if match_result:
				option_name, confidence = match_result
				logger.info(f"Intelligent matching found: '{option_name}' with confidence {confidence}")
				if confidence > 50:  # High confidence match
					embed, view = await self.guide_manager_ref.get_embed_for_selection(
						option_name, author_id=self.author_id
					)
					await interaction.response.edit_message(embed=embed, view=view)
					return

			# Fall back to regular search
			logger.debug("Falling back to regular search")
			results = await self.guide_manager_ref.search_content(query, self.author_id)

			if not results:
				logger.info(f"No results found for search query: '{query}'")
				# No results found
				embed = discord.Embed(
					title="🔍 No Results Found",
					description=f"Sorry, couldn't find anything for '{query}'",
					color=0xFF6B6B
				)

				# Suggest alternatives
				suggestions = self.search_manager.suggest_alternatives(query)
				if suggestions:
					suggestion_text = "\n".join([f"• {suggestion}" for suggestion in suggestions])
					embed.add_field(name="Try searching for:", value=suggestion_text, inline=False)
					logger.debug(f"Added {len(suggestions)} suggestions to no-results embed")

				embed.add_field(
					name="💡 Search Tips",
					value="• Use specific keywords\n• Try different terms\n• Browse categories from main menu",
					inline=False
				)
			else:
				logger.info(f"Found {len(results)} search results for query: '{query}'")
				# Show search results
				embed = discord.Embed(
					title=f"🔍 Search Results for '{query}'",
					description=f"Found {len(results)} matches:",
					color=0x4D0EB3
				)

				for i, result in enumerate(results[:5], 1):
					embed.add_field(
						name=f"{i}. {result['name']} ({result['score']}% match)",
						value=result['description'][:100] + ("..." if len(result['description']) > 100 else ""),
						inline=False
					)

			# Create view with search results
			view = SearchResultsView(results, self.author_id, self.guide_manager_ref)
			await interaction.response.edit_message(embed=embed, view=view)

		except Exception as e:
			logger.error(f"Error processing search modal submission: {e}")
			logger.exception("Search modal error details:")
			await interaction.response.send_message(
				"An error occurred while processing your search. Please try again.",
				ephemeral=True
			)


class SearchResultsView(discord.ui.View):
	"""View for displaying and selecting search results"""

	def __init__(self, results, author_id, guide_manager_ref):
		super().__init__(timeout=300)
		logger.debug(f"Initializing SearchResultsView for user {author_id} with {len(results)} results")
		self.results = results
		self.author_id = author_id
		self.guide_manager_ref = guide_manager_ref

		if results:
			# Add dropdown with results
			options = []
			for result in results[:10]:  # Discord limit
				options.append(discord.SelectOption(
					label=result['name'],
					description=result['description'][:100],
					value=result['name']
				))

			if options:
				self.add_item(SearchResultsDropdown(options, author_id, guide_manager_ref))
				logger.debug(f"Added dropdown with {len(options)} search result options")

		# Add navigation buttons
		self.add_item(MainMenuButton(author_id))
		self.add_item(NewSearchButton(author_id, guide_manager_ref))


class SearchResultsDropdown(discord.ui.Select):
	"""Dropdown for search result selection"""

	def __init__(self, options, author_id, guide_manager_ref):
		super().__init__(
			placeholder="Select a result...",
			options=options,
			custom_id="search_results_dropdown"
		)
		logger.debug(f"Initializing SearchResultsDropdown for user {author_id} with {len(options)} options")
		self.author_id = author_id
		self.guide_manager_ref = guide_manager_ref

	async def callback(self, interaction: discord.Interaction):
		if interaction.user.id != self.author_id:
			logger.warning(
				f"Unauthorized interaction attempt by user {interaction.user.id} on dropdown for user {self.author_id}")
			await interaction.response.send_message("You cannot interact with this menu.", ephemeral=True)
			return

		selection = self.values[0]
		logger.info(f"Search result selected by user {self.author_id}: '{selection}'")

		try:
			embed, view = await self.guide_manager_ref.get_embed_for_selection(
				selection, author_id=self.author_id, search_context=True
			)
			await interaction.response.edit_message(embed=embed, view=view)
		except Exception as e:
			logger.error(f"Error processing search result selection '{selection}': {e}")
			await interaction.response.send_message("Error loading selection. Please try again.", ephemeral=True)


class NewSearchButton(discord.ui.Button):
	"""Button to start a new search"""

	def __init__(self, author_id, guide_manager_ref):
		super().__init__(label="🔍 New Search", style=discord.ButtonStyle.secondary)
		logger.debug(f"Initializing NewSearchButton for user {author_id}")
		self.author_id = author_id
		self.guide_manager_ref = guide_manager_ref

	async def callback(self, interaction: discord.Interaction):
		if interaction.user.id != self.author_id:
			logger.warning(
				f"Unauthorized interaction attempt by user {interaction.user.id} on new search button for user {self.author_id}")
			await interaction.response.send_message("You cannot interact with this menu.", ephemeral=True)
			return

		logger.info(f"New search button clicked by user {self.author_id}")
		try:
			modal = SearchModal(
				self.guide_manager_ref.search_engine,
				self.author_id,
				self.guide_manager_ref
			)
			await interaction.response.send_modal(modal)
		except Exception as e:
			logger.error(f"Error opening new search modal: {e}")
			await interaction.response.send_message("Error opening search. Please try again.", ephemeral=True)


class EnhancedHistoryTrackingView(discord.ui.View):
	"""Enhanced view with improved navigation and features"""

	def __init__(self, options, current_option=None, history=None, author_id=None,
				 search_manager=None, quick_access_manager=None):
		super().__init__(timeout=600)  # Extended timeout
		logger.debug(f"Initializing EnhancedHistoryTrackingView for user {author_id} with {len(options)} options")
		self.current_option = current_option
		self.history = history or []
		self.author_id = author_id
		self.search_manager = search_manager
		self.quick_access_manager = quick_access_manager

		# Add main dropdown if options exist
		if options:
			self.add_item(HelpMenuDropdown(options, self.author_id))
			logger.debug(f"Added main dropdown with {len(options)} options")

		# Add enhanced navigation buttons
		if self.history:
			self.add_item(BackButton(self.history, self.author_id))
			logger.debug(f"Added back button (history depth: {len(self.history)})")

		if current_option:
			self.add_item(MainMenuButton(self.author_id))
			logger.debug("Added main menu button")

		# Add search functionality
		if search_manager:
			self.add_item(SearchButton(self.author_id, guide_manager))
			logger.debug("Added search button")

		# Add quick access if available
		if quick_access_manager and author_id:
			shortcuts = quick_access_manager.get_user_shortcuts(author_id)
			if shortcuts:
				self.add_item(QuickAccessButton(shortcuts, self.author_id))
				logger.debug(f"Added quick access button with {len(shortcuts)} shortcuts")


class SearchButton(discord.ui.Button):
	"""Button to open search modal"""

	def __init__(self, author_id, guide_manager_ref):
		super().__init__(label="🔍 Search", style=discord.ButtonStyle.primary, row=1)
		logger.debug(f"Initializing SearchButton for user {author_id}")
		self.author_id = author_id
		self.guide_manager_ref = guide_manager_ref

	async def callback(self, interaction: discord.Interaction):
		if interaction.user.id != self.author_id:
			logger.warning(
				f"Unauthorized interaction attempt by user {interaction.user.id} on search button for user {self.author_id}")
			await interaction.response.send_message("You cannot interact with this menu.", ephemeral=True)
			return

		logger.info(f"Search button clicked by user {self.author_id}")
		try:
			modal = SearchModal(
				self.guide_manager_ref.search_engine,
				self.author_id,
				self.guide_manager_ref
			)
			await interaction.response.send_modal(modal)
		except Exception as e:
			logger.error(f"Error opening search modal: {e}")
			await interaction.response.send_message("Error opening search. Please try again.", ephemeral=True)


class QuickAccessButton(discord.ui.Button):
	"""Button for quick access to frequently used content"""

	def __init__(self, shortcuts, author_id):
		super().__init__(label="⚡ Quick Access", style=discord.ButtonStyle.secondary, row=1)
		logger.debug(f"Initializing QuickAccessButton for user {author_id} with {len(shortcuts)} shortcuts")
		self.shortcuts = shortcuts
		self.author_id = author_id

	async def callback(self, interaction: discord.Interaction):
		if interaction.user.id != self.author_id:
			logger.warning(
				f"Unauthorized interaction attempt by user {interaction.user.id} on quick access button for user {self.author_id}")
			await interaction.response.send_message("You cannot interact with this menu.", ephemeral=True)
			return

		logger.info(f"Quick access button clicked by user {self.author_id}")

		# Create dropdown with shortcuts
		if self.shortcuts:
			options = []
			for shortcut in self.shortcuts[:10]:
				options.append(discord.SelectOption(
					label=shortcut,
					description="Quick access item",
					value=shortcut
				))

			view = discord.ui.View()
			dropdown = QuickAccessDropdown(options, self.author_id)
			view.add_item(dropdown)

			embed = discord.Embed(
				title="⚡ Your Quick Access",
				description="Select from your frequently accessed content:",
				color=0x00FF9F
			)

			logger.debug(f"Generated quick access menu with {len(options)} options for user {self.author_id}")
			await interaction.response.edit_message(embed=embed, view=view)
		else:
			logger.warning(f"Quick access button clicked but no shortcuts available for user {self.author_id}")
			await interaction.response.send_message("No quick access items available.", ephemeral=True)


class QuickAccessDropdown(discord.ui.Select):
	"""Dropdown for quick access selection"""

	def __init__(self, options, author_id):
		super().__init__(
			placeholder="Choose quick access item...",
			options=options,
			custom_id="quick_access_dropdown"
		)
		logger.debug(f"Initializing QuickAccessDropdown for user {author_id} with {len(options)} options")
		self.author_id = author_id

	async def callback(self, interaction: discord.Interaction):
		if interaction.user.id != self.author_id:
			logger.warning(
				f"Unauthorized interaction attempt by user {interaction.user.id} on quick access dropdown for user {self.author_id}")
			await interaction.response.send_message("You cannot interact with this menu.", ephemeral=True)
			return

		selection = self.values[0]
		logger.info(f"Quick access item selected by user {self.author_id}: '{selection}'")

		try:
			embed, view = await guide_manager.get_embed_for_selection(
				selection, author_id=self.author_id
			)
			await interaction.response.edit_message(embed=embed, view=view)
		except Exception as e:
			logger.error(f"Error processing quick access selection '{selection}': {e}")
			await interaction.response.send_message("Error loading selection. Please try again.", ephemeral=True)


class BackButton(discord.ui.Button):
	"""Enhanced back button with breadcrumb awareness"""

	def __init__(self, history, author_id):
		super().__init__(label="← Back", style=discord.ButtonStyle.secondary, row=2)
		logger.debug(f"Initializing BackButton for user {author_id} (history depth: {len(history)})")
		self.history = history
		self.author_id = author_id

	async def callback(self, interaction: discord.Interaction):
		if interaction.user.id != self.author_id:
			logger.warning(
				f"Unauthorized interaction attempt by user {interaction.user.id} on back button for user {self.author_id}")
			await interaction.response.send_message("You cannot interact with this menu.", ephemeral=True)
			return

		logger.info(f"Back button clicked by user {self.author_id}")

		if not self.history:
			logger.warning(f"Back button clicked but no history available for user {self.author_id}")
			await interaction.response.send_message("No previous page to go back to.", ephemeral=True)
			return

		previous_option = self.history.pop()
		logger.debug(f"Going back to previous option: '{previous_option}' for user {self.author_id}")

		# Update breadcrumb
		current_path = guide_manager.navigation.get_navigation_path(self.author_id)
		if current_path and len(current_path) > 1:
			current_path.pop()
			guide_manager.navigation.update_breadcrumb(self.author_id, current_path)

		try:
			embed, view = await guide_manager.get_embed_for_selection(
				previous_option, author_id=self.author_id
			)
			await interaction.response.edit_message(embed=embed, view=view)
		except Exception as e:
			logger.error(f"Failed to load previous view '{previous_option}': {e}")
			await interaction.response.send_message("Error loading previous page.", ephemeral=True)


class MainMenuButton(discord.ui.Button):
	"""Enhanced main menu button"""

	def __init__(self, author_id):
		super().__init__(label="🏠 Main Menu", style=discord.ButtonStyle.success, row=2)
		logger.debug(f"Initializing MainMenuButton for user {author_id}")
		self.author_id = author_id

	async def callback(self, interaction: discord.Interaction):
		if interaction.user.id != self.author_id:
			logger.warning(
				f"Unauthorized interaction attempt by user {interaction.user.id} on main menu button for user {self.author_id}")
			await interaction.response.send_message("You cannot interact with this menu.", ephemeral=True)
			return

		logger.info(f"Main menu button clicked by user {self.author_id}")

		# Reset breadcrumb
		guide_manager.navigation.update_breadcrumb(self.author_id, ["Main Menu"])

		try:
			embed, view = await guide_manager.get_help_menu(author_id=self.author_id)
			await interaction.response.edit_message(embed=embed, view=view)
		except Exception as e:
			logger.error(f"Error loading main menu for user {self.author_id}: {e}")
			await interaction.response.send_message("Error loading main menu.", ephemeral=True)


class HelpMenuDropdown(discord.ui.Select):
	"""Enhanced dropdown with better option handling"""

	def __init__(self, options, author_id):
		logger.debug(f"Initializing HelpMenuDropdown for user {author_id} with {len(options)} options")

		sanitized_options = [
			option if isinstance(option, dict) else {
				"name": option.label,
				"meta_description": option.description,
			}
			for option in options
		]

		select_options = []
		for option in sanitized_options[:25]:  # Discord limit
			description = option.get("meta_description", "No description available.")
			if len(description) > 100:
				description = description[:97] + "..."

			select_options.append(discord.SelectOption(
				label=option["name"][:100],  # Discord limit
				description=description,
				value=option["name"]
			))

		super().__init__(
			placeholder="Choose an option...",
			min_values=1,
			max_values=1,
			options=select_options,
			custom_id="help_menu_dropdown"
		)
		self.author_id = author_id
		logger.debug(f"Created help menu dropdown with {len(select_options)} Discord options")

	async def callback(self, interaction: discord.Interaction):
		if interaction.user.id != self.author_id:
			logger.warning(
				f"Unauthorized interaction attempt by user {interaction.user.id} on help menu dropdown for user {self.author_id}")
			await interaction.response.send_message("You cannot interact with this menu.", ephemeral=True)
			return

		selection = self.values[0]
		logger.info(f"Help menu option selected by user {self.author_id}: '{selection}'")

		try:
			embed, view = await guide_manager.get_embed_for_selection(
				selection, author_id=self.author_id
			)
			await interaction.response.edit_message(embed=embed, view=view)
		except Exception as e:
			logger.error(f"Error processing selection '{selection}': {e}")
			await interaction.response.send_message(
				"Something went wrong while loading that option. Please try again.",
				ephemeral=True
			)


# Wrapper functions for backwards compatibility
async def get_help_menu(author_id=None):
	"""Wrapper function for backwards compatibility"""
	logger.debug(f"Wrapper function get_help_menu called for user {author_id}")
	return await guide_manager.get_help_menu(author_id)


async def get_embed_for_selection(option_name, author_id=None):
	"""Wrapper function for backwards compatibility"""
	logger.debug(f"Wrapper function get_embed_for_selection called for '{option_name}' (user: {author_id})")
	return await guide_manager.get_embed_for_selection(option_name, author_id)


async def search_nested_options(options, option_name, author_id=None):
	"""Wrapper function for backwards compatibility"""
	logger.debug(f"Wrapper function search_nested_options called for '{option_name}' (user: {author_id})")
	return await guide_manager.search_nested_options(options, option_name, author_id)


logger.info("Guide module loaded successfully with enhanced logging")