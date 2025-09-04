import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime
from typing import Optional, List, Dict, Any, cast
import uuid
import json
import io
import csv
import os
from dotenv import load_dotenv
from utils.logger import get_logger, log_context, PerformanceLogger
from Database.DatabaseManager import db_manager

logger = get_logger("Suggestion")

load_dotenv()


class SuggestionView(discord.ui.View):
	def __init__(self, suggestion_id: str, db_manager):
		super().__init__(timeout=None)
		self.suggestion_id = suggestion_id
		self.db_manager = db_manager

	@discord.ui.button(label="👍", style=cast(discord.ButtonStyle, discord.ButtonStyle.success), custom_id="upvote")
	async def upvote(self, interaction: discord.Interaction, button: discord.ui.Button):
		logger.debug(f"Upvote button clicked by user {interaction.user.id} for suggestion {self.suggestion_id}")
		await self._handle_vote(interaction, "upvote")

	@discord.ui.button(label="👎", style=cast(discord.ButtonStyle, discord.ButtonStyle.danger), custom_id="downvote")
	async def downvote(self, interaction: discord.Interaction, button: discord.ui.Button):
		logger.debug(f"Downvote button clicked by user {interaction.user.id} for suggestion {self.suggestion_id}")
		await self._handle_vote(interaction, "downvote")

	@discord.ui.button(label="❤️", style=cast(discord.ButtonStyle, discord.ButtonStyle.primary), custom_id="love")
	async def love(self, interaction: discord.Interaction, button: discord.ui.Button):
		logger.debug(f"Love button clicked by user {interaction.user.id} for suggestion {self.suggestion_id}")
		await self._handle_vote(interaction, "love")

	@discord.ui.button(label="🤔", style=cast(discord.ButtonStyle, discord.ButtonStyle.secondary), custom_id="thinking")
	async def thinking(self, interaction: discord.Interaction, button: discord.ui.Button):
		logger.debug(f"Thinking button clicked by user {interaction.user.id} for suggestion {self.suggestion_id}")
		await self._handle_vote(interaction, "thinking")

	async def _handle_vote(self, interaction: discord.Interaction, vote_type: str):
		with PerformanceLogger(logger, f"handle_vote_{vote_type}"):
			user_id = interaction.user.id
			logger.info(f"Processing {vote_type} vote from user {user_id} for suggestion {self.suggestion_id}")

			try:
				result = await self.db_manager.add_vote(self.suggestion_id, user_id, vote_type)

				if result["success"]:
					logger.info(f"Vote processed successfully: {result['message']}")
					vote_counts = await self.db_manager.get_vote_counts(self.suggestion_id)
					embed = interaction.message.embeds[0] if interaction.message.embeds else None

					if embed:
						# Update vote counts in embed
						vote_display = f"👍 {vote_counts.get('upvote', 0)} | 👎 {vote_counts.get('downvote', 0)} | ❤️ {vote_counts.get('love', 0)} | 🤔 {vote_counts.get('thinking', 0)}"

						# Update or add vote field
						for i, field in enumerate(embed.fields):
							if field.name == "Votes":
								embed.set_field_at(i, name="Votes", value=vote_display, inline=False)
								break
						else:
							embed.add_field(name="Votes", value=vote_display, inline=False)

						await interaction.response.edit_message(embed=embed, view=self)
						logger.debug(f"Updated embed with new vote counts: {vote_display}")
					else:
						await interaction.response.send_message(f"✅ {result['message']}", ephemeral=True)
				else:
					logger.warning(f"Vote processing failed: {result['message']}")
					await interaction.response.send_message(f"❌ {result['message']}", ephemeral=True)

			except Exception as e:
				logger.error(f"Error handling vote: {e}", exc_info=True)
				if not interaction.response.is_done():
					await interaction.response.send_message("❌ An error occurred while processing your vote.",
															ephemeral=True)


class SuggestionModal(discord.ui.Modal):
	def __init__(self, template_type: str):
		super().__init__(title=f"{template_type} Suggestion")
		self.template_type = template_type
		logger.debug(f"SuggestionModal initialized for template type: {template_type}")

		templates = {
			"Bot Feature": {
				"title": "Feature Request",
				"description": "Describe the bot feature you'd like to see",
				"use_case": "How would this feature be used?",
				"priority": "How important is this feature? (1-10)"
			},
			"Server Rule": {
				"title": "Rule Suggestion",
				"description": "What rule change would you like to propose?",
				"use_case": "Why is this rule needed?",
				"priority": "How urgent is this change? (1-10)"
			},
			"Event Proposal": {
				"title": "Event Idea",
				"description": "Describe the event you'd like to organize",
				"use_case": "When should this event happen?",
				"priority": "How much interest do you think this will generate? (1-10)"
			},
			"Channel Request": {
				"title": "Channel Request",
				"description": "What type of channel would you like added?",
				"use_case": "What would this channel be used for?",
				"priority": "How needed is this channel? (1-10)"
			}
		}

		template = templates.get(template_type, templates["Bot Feature"])

		self.title_input = discord.ui.TextInput(
			label="Title",
			placeholder=template["title"],
			max_length=100
		)
		self.description_input = discord.ui.TextInput(
			label="Description",
			placeholder=template["description"],
			style=discord.TextStyle.paragraph,
			max_length=1000
		)
		self.use_case_input = discord.ui.TextInput(
			label="Use Case/Reasoning",
			placeholder=template["use_case"],
			style=discord.TextStyle.paragraph,
			max_length=500
		)
		self.priority_input = discord.ui.TextInput(
			label="Priority",
			placeholder=template["priority"],
			max_length=2
		)

		self.add_item(self.title_input)
		self.add_item(self.description_input)
		self.add_item(self.use_case_input)
		self.add_item(self.priority_input)

	async def on_submit(self, interaction: discord.Interaction):
		with log_context(logger, f"template_submission_{self.template_type}"):
			# Combine all inputs into suggestion text
			suggestion_text = f"**{self.title_input.value}**\n\n{self.description_input.value}\n\n**Use Case:** {self.use_case_input.value}\n\n**Priority:** {self.priority_input.value}"

			logger.info(
				f"Template suggestion submitted by user {interaction.user.id} - Type: {self.template_type}, Length: {len(suggestion_text)} chars")

			# Get the suggestion cog and call its suggest method
			cog = interaction.client.get_cog("SuggestionCog")
			if cog:
				await cog._process_suggestion(interaction, suggestion_text, False, self.template_type)
			else:
				logger.error("SuggestionCog not found when processing template submission")
				await interaction.response.send_message("❌ System error: Suggestion service unavailable.",
														ephemeral=True)


class SuggestionDatabaseManager:
	"""
	Database manager adapter that uses the new DatabaseManager for suggestions.
	This maintains backward compatibility while using the new database architecture.
	"""

	def __init__(self, mongo_uri: str = None):
		logger.info("Initializing SuggestionDatabaseManager with new DatabaseManager")
		# We don't need the mongo_uri parameter anymore since we use the global db_manager
		self.db_manager = db_manager
		self._initialized = False

	async def _ensure_initialized(self):
		"""Ensure the database manager is initialized"""
		if not self._initialized:
			if not self.db_manager._initialized:
				await self.db_manager.initialize()
			self._initialized = True

	async def create_suggestion(self, user_id: int, text: str, anonymous: bool = False,
								category: str = "Other", message_id: int = None,
								thread_id: int = None) -> str:
		"""Create a new suggestion in the database"""
		await self._ensure_initialized()

		with PerformanceLogger(logger, "create_suggestion"):
			suggestion_id = str(uuid.uuid4())

			logger.info(
				f"Creating suggestion for user {user_id if not anonymous else 'anonymous'} - Category: {category}, Length: {len(text)} chars")

			suggestion_doc = {
				"suggestion_id": suggestion_id,
				"user_id": user_id if not anonymous else None,
				"text": text,
				"anonymous": anonymous,
				"category": category,
				"status": "Pending",
				"priority": "Medium",
				"message_id": message_id,
				"thread_id": thread_id,
				"admin_notes": "",
				"implementation_date": None,
				"tags": []
			}

			try:
				await self.db_manager.suggestions_suggestions.create_one(suggestion_doc)

				# Update user statistics
				if not anonymous:
					await self._update_user_stats(user_id, "suggestions_submitted")

				logger.info(f"Successfully created suggestion {suggestion_id} for user {user_id}")
				return suggestion_id

			except Exception as e:
				logger.error(f"Error creating suggestion: {e}", exc_info=True)
				raise

	async def update_suggestion_status(self, suggestion_id: str, status: str,
									   admin_id: int, reason: str = None) -> bool:
		"""Update suggestion status"""
		await self._ensure_initialized()

		with PerformanceLogger(logger, "update_suggestion_status"):
			logger.info(f"Admin {admin_id} updating suggestion {suggestion_id} status to {status}")

			try:
				update_doc = {
					"status": status,
					"last_updated_by": admin_id
				}

				if reason:
					update_doc["status_reason"] = reason
					logger.debug(f"Status update reason: {reason}")

				result = await self.db_manager.suggestions_suggestions.update_one(
					{"suggestion_id": suggestion_id},
					{"$set": update_doc}
				)

				if result:
					# Add to notification queue
					suggestion = await self.db_manager.suggestions_suggestions.find_one(
						{"suggestion_id": suggestion_id})
					if suggestion and not suggestion.get("anonymous") and suggestion.get("user_id"):
						await self._queue_notification(suggestion["user_id"], suggestion_id, status, reason)

					logger.info(f"Successfully updated suggestion {suggestion_id} status to {status}")
					return True
				else:
					logger.warning(f"No suggestion found with ID {suggestion_id} to update")
					return False

			except Exception as e:
				logger.error(f"Error updating suggestion status: {e}", exc_info=True)
				return False

	async def add_vote(self, suggestion_id: str, user_id: int, vote_type: str) -> Dict[str, Any]:
		"""Add or update a vote for a suggestion"""
		await self._ensure_initialized()

		with PerformanceLogger(logger, "add_vote"):
			logger.debug(f"Processing {vote_type} vote from user {user_id} for suggestion {suggestion_id}")

			try:
				# Check if user already voted
				existing_vote = await self.db_manager.suggestions_votes.find_one({
					"suggestion_id": suggestion_id,
					"user_id": user_id
				})

				vote_doc = {
					"suggestion_id": suggestion_id,
					"user_id": user_id,
					"vote_type": vote_type
				}

				if existing_vote:
					if existing_vote["vote_type"] == vote_type:
						# Remove vote if same type
						await self.db_manager.suggestions_votes.delete_one({
							"suggestion_id": suggestion_id,
							"user_id": user_id
						})
						logger.info(f"Removed {vote_type} vote from user {user_id} for suggestion {suggestion_id}")
						return {"success": True, "message": f"Removed your {vote_type} vote"}
					else:
						# Update vote type
						await self.db_manager.suggestions_votes.update_one(
							{"suggestion_id": suggestion_id, "user_id": user_id},
							{"$set": {"vote_type": vote_type}}
						)
						logger.info(
							f"Changed vote from {existing_vote['vote_type']} to {vote_type} for user {user_id} on suggestion {suggestion_id}")
						return {"success": True, "message": f"Changed vote to {vote_type}"}
				else:
					# Add new vote
					await self.db_manager.suggestions_votes.create_one(vote_doc)
					await self._update_user_stats(user_id, "votes_cast")
					logger.info(f"Added new {vote_type} vote from user {user_id} for suggestion {suggestion_id}")
					return {"success": True, "message": f"Added {vote_type} vote"}

			except Exception as e:
				logger.error(f"Error adding vote: {e}", exc_info=True)
				return {"success": False, "message": "Failed to process vote"}

	async def get_vote_counts(self, suggestion_id: str) -> Dict[str, int]:
		"""Get vote counts for a suggestion"""
		await self._ensure_initialized()

		with PerformanceLogger(logger, "get_vote_counts"):
			try:
				pipeline = [
					{"$match": {"suggestion_id": suggestion_id}},
					{"$group": {"_id": "$vote_type", "count": {"$sum": 1}}}
				]

				results = await self.db_manager.suggestions_votes.aggregate(pipeline)
				vote_counts = {result["_id"]: result["count"] for result in results}

				logger.debug(f"Retrieved vote counts for suggestion {suggestion_id}: {vote_counts}")
				return vote_counts

			except Exception as e:
				logger.error(f"Error getting vote counts: {e}", exc_info=True)
				return {}

	async def search_suggestions(self, query: str = None, category: str = None,
								 status: str = None, author_id: int = None,
								 limit: int = 10) -> List[Dict]:
		"""Search suggestions with filters"""
		await self._ensure_initialized()

		with PerformanceLogger(logger, "search_suggestions"):
			search_params = {
				"query": query,
				"category": category,
				"status": status,
				"author_id": author_id,
				"limit": limit
			}
			logger.info(f"Searching suggestions with parameters: {search_params}")

			try:
				filter_doc = {}

				if query:
					filter_doc["$text"] = {"$search": query}
				if category and category != "All":
					filter_doc["category"] = category
				if status and status != "All":
					filter_doc["status"] = status
				if author_id:
					filter_doc["user_id"] = author_id

				results = await self.db_manager.suggestions_suggestions.find_many(
					filter_dict=filter_doc,
					limit=limit,
					sort=[("created_at", -1)]
				)

				logger.info(f"Search returned {len(results)} suggestions")
				return results

			except Exception as e:
				logger.error(f"Error searching suggestions: {e}", exc_info=True)
				return []

	async def get_user_suggestions(self, user_id: int, limit: int = 10) -> List[Dict]:
		"""Get suggestions by a specific user"""
		await self._ensure_initialized()

		with PerformanceLogger(logger, "get_user_suggestions"):
			logger.info(f"Retrieving suggestions for user {user_id} (limit: {limit})")

			try:
				results = await self.db_manager.suggestions_suggestions.find_many(
					filter_dict={"user_id": user_id},
					limit=limit,
					sort=[("created_at", -1)]
				)
				logger.info(f"Found {len(results)} suggestions for user {user_id}")
				return results
			except Exception as e:
				logger.error(f"Error getting user suggestions: {e}", exc_info=True)
				return []

	async def get_suggestion_stats(self) -> Dict[str, Any]:
		"""Get overall suggestion statistics"""
		await self._ensure_initialized()

		with PerformanceLogger(logger, "get_suggestion_stats"):
			logger.info("Generating suggestion statistics")

			try:
				total_suggestions = await self.db_manager.suggestions_suggestions.count_documents({})

				# Status distribution
				status_pipeline = [
					{"$group": {"_id": "$status", "count": {"$sum": 1}}}
				]
				status_results = await self.db_manager.suggestions_suggestions.aggregate(status_pipeline)
				status_dist = {result["_id"]: result["count"] for result in status_results}

				# Category distribution
				category_pipeline = [
					{"$group": {"_id": "$category", "count": {"$sum": 1}}}
				]
				category_results = await self.db_manager.suggestions_suggestions.aggregate(category_pipeline)
				category_dist = {result["_id"]: result["count"] for result in category_results}

				# Top contributors
				contributor_pipeline = [
					{"$match": {"anonymous": False}},
					{"$group": {"_id": "$user_id", "count": {"$sum": 1}}},
					{"$sort": {"count": -1}},
					{"$limit": 5}
				]
				contributor_results = await self.db_manager.suggestions_suggestions.aggregate(contributor_pipeline)

				stats = {
					"total_suggestions": total_suggestions,
					"status_distribution": status_dist,
					"category_distribution": category_dist,
					"top_contributors": contributor_results
				}

				logger.info(
					f"Generated stats: {total_suggestions} total suggestions, {len(status_dist)} statuses, {len(category_dist)} categories")
				return stats

			except Exception as e:
				logger.error(f"Error getting suggestion stats: {e}", exc_info=True)
				return {}

	async def _update_user_stats(self, user_id: int, stat_type: str):
		"""Update user statistics"""
		try:
			await self.db_manager.get_collection_manager('suggestions_userstats').update_one(
				{"user_id": user_id},
				{
					"$inc": {stat_type: 1},
					"$set": {"last_activity": datetime.utcnow()}
				},
				upsert=True
			)
			logger.debug(f"Updated {stat_type} stat for user {user_id}")
		except Exception as e:
			logger.error(f"Error updating user stats: {e}", exc_info=True)

	async def _queue_notification(self, user_id: int, suggestion_id: str,
								  status: str, reason: str = None):
		"""Queue notification for user"""
		try:
			notification_doc = {
				"user_id": user_id,
				"suggestion_id": suggestion_id,
				"type": "status_update",
				"status": status,
				"reason": reason,
				"sent": False
			}

			await self.db_manager.get_collection_manager('suggestions_notification_queue').create_one(notification_doc)
			logger.info(
				f"Queued notification for user {user_id} - suggestion {suggestion_id} status changed to {status}")

		except Exception as e:
			logger.error(f"Error queuing notification: {e}", exc_info=True)

	async def get_pending_notifications(self) -> List[Dict]:
		"""Get pending notifications"""
		await self._ensure_initialized()

		try:
			notifications = await self.db_manager.get_collection_manager('suggestions_notification_queue').find_many(
				{"sent": False})
			logger.debug(f"Retrieved {len(notifications)} pending notifications")
			return notifications
		except Exception as e:
			logger.error(f"Error getting pending notifications: {e}", exc_info=True)
			return []

	async def mark_notification_sent(self, notification_id):
		"""Mark notification as sent"""
		try:
			await self.db_manager.get_collection_manager('suggestions_notification_queue').update_one(
				{"_id": notification_id},
				{"$set": {"sent": True, "sent_at": datetime.utcnow()}}
			)
			logger.debug(f"Marked notification {notification_id} as sent")
		except Exception as e:
			logger.error(f"Error marking notification as sent: {e}", exc_info=True)

	# Legacy compatibility methods for direct collection access
	@property
	def suggestions(self):
		"""Legacy access to suggestions collection"""
		return self.db_manager.get_raw_collection('Suggestions', 'Suggestions')


class SuggestionCommandGroup(app_commands.Group):
	"""Command group for suggestion commands"""

	def __init__(self, cog):
		super().__init__(name="suggest", description="Suggestion system commands")
		self.cog = cog

	@app_commands.command(name="submit", description="Submit a suggestion")
	@app_commands.describe(
		suggestion_text="The text of your suggestion",
		anonymous="Submit anonymously",
		category="Category for your suggestion"
	)
	@app_commands.choices(category=[
		app_commands.Choice(name="Bot Feature", value="Bot Feature"),
		app_commands.Choice(name="Server Improvement", value="Server Improvement"),
		app_commands.Choice(name="Event Idea", value="Event Idea"),
		app_commands.Choice(name="Rule Change", value="Rule Change"),
		app_commands.Choice(name="Other", value="Other")
	])
	@app_commands.checks.cooldown(1, 30)
	async def submit_suggestion(
			self,
			interaction: discord.Interaction,
			suggestion_text: str,
			anonymous: bool = False,
			category: str = "Other"
	):
		"""Submit a suggestion"""
		logger.info(
			f"Suggestion submission command used by {interaction.user.id} - Category: {category}, Anonymous: {anonymous}")
		await self.cog._process_suggestion(interaction, suggestion_text, anonymous, category)

	@app_commands.command(name="template", description="Use a suggestion template")
	@app_commands.describe(template_type="Choose a template type")
	@app_commands.choices(template_type=[
		app_commands.Choice(name="Bot Feature", value="Bot Feature"),
		app_commands.Choice(name="Server Rule", value="Server Rule"),
		app_commands.Choice(name="Event Proposal", value="Event Proposal"),
		app_commands.Choice(name="Channel Request", value="Channel Request")
	])
	async def submit_template(self, interaction: discord.Interaction, template_type: str):
		"""Submit suggestion using template"""
		logger.info(f"Template submission command used by {interaction.user.id} - Template: {template_type}")
		modal = SuggestionModal(template_type)
		await interaction.response.send_modal(modal)

	@app_commands.command(name="search", description="Search suggestions")
	@app_commands.describe(
		query="Search terms",
		category="Filter by category",
		status="Filter by status",
		author="Filter by author (mention them)"
	)
	@app_commands.choices(
		category=[
			app_commands.Choice(name="All", value="All"),
			app_commands.Choice(name="Bot Feature", value="Bot Feature"),
			app_commands.Choice(name="Server Improvement", value="Server Improvement"),
			app_commands.Choice(name="Event Idea", value="Event Idea"),
			app_commands.Choice(name="Rule Change", value="Rule Change"),
			app_commands.Choice(name="Other", value="Other")
		],
		status=[
			app_commands.Choice(name="All", value="All"),
			app_commands.Choice(name="Pending", value="Pending"),
			app_commands.Choice(name="Under Review", value="Under Review"),
			app_commands.Choice(name="Approved", value="Approved"),
			app_commands.Choice(name="Implemented", value="Implemented"),
			app_commands.Choice(name="Rejected", value="Rejected"),
			app_commands.Choice(name="On Hold", value="On Hold")
		]
	)
	async def search_suggestions(
			self,
			interaction: discord.Interaction,
			query: Optional[str] = None,
			category: Optional[str] = None,
			status: Optional[str] = None,
			author: Optional[discord.Member] = None
	):
		"""Search through suggestions"""
		logger.info(
			f"Search command used by {interaction.user.id} - Query: '{query}', Category: {category}, Status: {status}, Author: {author.id if author else None}")
		await interaction.response.defer()

		results = await self.cog.db_manager.search_suggestions(
			query, category, status, author.id if author else None, limit=10
		)

		if not results:
			logger.info(f"Search returned no results for user {interaction.user.id}")
			await interaction.followup.send("❌ No suggestions found matching your criteria.")
			return

		embed = discord.Embed(
			title="🔍 Suggestion Search Results",
			color=discord.Color.blue()
		)

		for i, suggestion in enumerate(results[:5], 1):
			text_preview = suggestion["text"][:100] + "..." if len(suggestion["text"]) > 100 else suggestion["text"]

			embed.add_field(
				name=f"{i}. {suggestion['category']} - {suggestion['status']}",
				value=f"**ID:** {suggestion['suggestion_id'][:8]}\n{text_preview}",
				inline=False
			)

		embed.set_footer(text=f"Showing {len(results[:5])} of {len(results)} results")
		logger.info(f"Search results displayed to user {interaction.user.id}: {len(results)} total results")

		await interaction.followup.send(embed=embed)

	@app_commands.command(name="mine", description="View your suggestion history")
	async def my_suggestions(self, interaction: discord.Interaction):
		"""View user's suggestion history"""
		logger.info(f"User {interaction.user.id} requested their suggestion history")
		await interaction.response.defer(ephemeral=True)

		suggestions = await self.cog.db_manager.get_user_suggestions(interaction.user.id)

		if not suggestions:
			logger.info(f"User {interaction.user.id} has no suggestions")
			await interaction.followup.send("You haven't submitted any suggestions yet.", ephemeral=True)
			return

		embed = discord.Embed(
			title="📝 Your Suggestions",
			color=discord.Color.green()
		)

		for i, suggestion in enumerate(suggestions[:5], 1):
			text_preview = suggestion["text"][:80] + "..." if len(suggestion["text"]) > 80 else suggestion["text"]
			vote_counts = await self.cog.db_manager.get_vote_counts(suggestion["suggestion_id"])
			total_votes = sum(vote_counts.values())

			embed.add_field(
				name=f"{i}. {suggestion['status']} - {suggestion['category']}",
				value=f"**ID:** {suggestion['suggestion_id'][:8]}\n{text_preview}\n**Votes:** {total_votes}",
				inline=False
			)

		embed.set_footer(text=f"Showing {len(suggestions[:5])} of {len(suggestions)} suggestions")
		logger.info(f"Displayed {len(suggestions)} suggestions to user {interaction.user.id}")

		await interaction.followup.send(embed=embed, ephemeral=True)


class SuggestionAdminGroup(app_commands.Group):
	"""Command group for suggestion admin commands"""

	def __init__(self, cog):
		super().__init__(name="suggestion-admin", description="Admin commands for suggestion system")
		self.cog = cog

	@app_commands.command(name="status", description="Update suggestion status (Admin only)")
	@app_commands.describe(
		suggestion_id="The ID of the suggestion (first 8 characters)",
		status="New status",
		reason="Reason for status change"
	)
	@app_commands.choices(status=[
		app_commands.Choice(name="Under Review", value="Under Review"),
		app_commands.Choice(name="Approved", value="Approved"),
		app_commands.Choice(name="Implemented", value="Implemented"),
		app_commands.Choice(name="Rejected", value="Rejected"),
		app_commands.Choice(name="On Hold", value="On Hold")
	])
	@app_commands.default_permissions(manage_guild=True)
	async def update_status(
			self,
			interaction: discord.Interaction,
			suggestion_id: str,
			status: str,
			reason: Optional[str] = None
	):
		"""Update suggestion status"""
		logger.info(f"Admin {interaction.user.id} attempting to update suggestion {suggestion_id} to status {status}")
		await interaction.response.defer(ephemeral=True)

		# Find full suggestion ID
		suggestions = await self.cog.db_manager.search_suggestions(limit=1000)
		full_id = None

		for suggestion in suggestions:
			if suggestion["suggestion_id"].startswith(suggestion_id):
				full_id = suggestion["suggestion_id"]
				break

		if not full_id:
			logger.warning(f"Admin {interaction.user.id} attempted to update non-existent suggestion {suggestion_id}")
			await interaction.followup.send("❌ Suggestion not found.", ephemeral=True)
			return

		success = await self.cog.db_manager.update_suggestion_status(
			full_id, status, interaction.user.id, reason
		)

		if success:
			logger.info(f"Admin {interaction.user.id} successfully updated suggestion {suggestion_id} to {status}")

			# Try to update the original suggestion embed in the suggestions channel
			try:
				# Retrieve the full suggestion document to get message/thread IDs
				doc = await db_manager.suggestions_suggestions.find_one({"suggestion_id": full_id})
				if doc and doc.get("message_id"):
					channel = self.cog.bot.get_channel(self.cog.suggestions_channel_id)
					if channel:
						message = await channel.fetch_message(doc["message_id"])
						if message and message.embeds:
							embed = message.embeds[0]

							# Update the "Status" field
							for i, field in enumerate(embed.fields):
								if field.name == "Status":
									embed.set_field_at(i, name="Status", value=status, inline=True)
									break
							else:
								embed.add_field(name="Status", value=status, inline=True)

							# Update embed color based on status
							status_colors = {
								"Pending": discord.Color.blue(),
								"Under Review": discord.Color.orange(),
								"Approved": discord.Color.green(),
								"Implemented": discord.Color.gold(),
								"Rejected": discord.Color.red(),
								"On Hold": discord.Color.purple(),
							}
							embed.color = status_colors.get(status, discord.Color.blue())

							await message.edit(embed=embed)

					# Optionally rename the thread to reflect status change
					if doc.get("thread_id"):
						thread = self.cog.bot.get_channel(doc["thread_id"])
						if thread and hasattr(thread, "edit"):
							try:
								await thread.edit(name=f"[{status}] {thread.name}")
							except Exception as thread_edit_err:
								logger.warning(f"Could not rename thread {doc['thread_id']}: {thread_edit_err}")

			except Exception as edit_err:
				logger.error(f"Failed to update suggestion embed for {full_id}: {edit_err}", exc_info=True)

			await interaction.followup.send(
				f"✅ Updated suggestion {suggestion_id} to **{status}**" +
				(f"\nReason: {reason}" if reason else ""),
				ephemeral=True
			)

		else:
			logger.error(f"Failed to update suggestion {suggestion_id} by admin {interaction.user.id}")
			await interaction.followup.send("❌ Failed to update suggestion.", ephemeral=True)

	@app_commands.command(name="stats", description="View suggestion statistics (Admin only)")
	@app_commands.default_permissions(manage_guild=True)
	async def suggestion_stats(self, interaction: discord.Interaction):
		"""View suggestion statistics"""
		logger.info(f"Admin {interaction.user.id} requested suggestion statistics")
		await interaction.response.defer()

		stats = await self.cog.db_manager.get_suggestion_stats()

		if not stats:
			logger.warning("No statistics available when requested by admin")
			await interaction.followup.send("❌ No statistics available.")
			return

		embed = discord.Embed(
			title="📊 Suggestion Statistics",
			color=discord.Color.blue()
		)

		embed.add_field(
			name="Total Suggestions",
			value=str(stats.get("total_suggestions", 0)),
			inline=True
		)

		# Status distribution
		status_dist = stats.get("status_distribution", {})
		if status_dist:
			status_text = "\n".join([f"{status}: {count}" for status, count in status_dist.items()])
			embed.add_field(name="By Status", value=status_text, inline=True)

		# Category distribution
		category_dist = stats.get("category_distribution", {})
		if category_dist:
			category_text = "\n".join([f"{cat}: {count}" for cat, count in category_dist.items()])
			embed.add_field(name="By Category", value=category_text, inline=True)

		# Top contributors
		contributors = stats.get("top_contributors", [])
		if contributors:
			contributor_text = ""
			for contrib in contributors[:5]:
				user = self.cog.bot.get_user(contrib["_id"])
				username = user.display_name if user else f"User {contrib['_id']}"
				contributor_text += f"{username}: {contrib['count']}\n"
			embed.add_field(name="Top Contributors", value=contributor_text, inline=False)

		logger.info(f"Statistics displayed to admin {interaction.user.id}")
		await interaction.followup.send(embed=embed)

	@app_commands.command(name="export", description="Export suggestions (Admin only)")
	@app_commands.describe(format_type="Export format")
	@app_commands.choices(format_type=[
		app_commands.Choice(name="CSV", value="CSV"),
		app_commands.Choice(name="JSON", value="JSON")
	])
	@app_commands.default_permissions(manage_guild=True)
	async def export_suggestions(self, interaction: discord.Interaction, format_type: str):
		"""Export suggestions to file"""
		logger.info(f"Admin {interaction.user.id} requested export in {format_type} format")
		await interaction.response.defer(ephemeral=True)

		suggestions = await self.cog.db_manager.search_suggestions(limit=1000)

		if not suggestions:
			logger.warning(f"No suggestions available for export requested by admin {interaction.user.id}")
			await interaction.followup.send("❌ No suggestions to export.", ephemeral=True)
			return

		try:
			if format_type == "CSV":
				# Create CSV
				output = io.StringIO()
				writer = csv.writer(output)

				# Headers
				writer.writerow([
					"ID", "User ID", "Text", "Category", "Status", "Anonymous",
					"Created At", "Updated At"
				])

				# Data
				for suggestion in suggestions:
					writer.writerow([
						suggestion.get("suggestion_id", ""),
						suggestion.get("user_id", ""),
						suggestion.get("text", ""),
						suggestion.get("category", ""),
						suggestion.get("status", ""),
						suggestion.get("anonymous", False),
						suggestion.get("created_at", ""),
						suggestion.get("updated_at", "")
					])

				file_content = output.getvalue().encode('utf-8')
				file = discord.File(io.BytesIO(file_content), filename="suggestions.csv")

			else:  # JSON
				# Prepare JSON data
				json_data = []
				for suggestion in suggestions:
					# Convert datetime objects to strings
					suggestion_copy = suggestion.copy()
					for key, value in suggestion_copy.items():
						if isinstance(value, datetime):
							suggestion_copy[key] = value.isoformat()
					json_data.append(suggestion_copy)

				json_content = json.dumps(json_data, indent=2).encode('utf-8')
				file = discord.File(io.BytesIO(json_content), filename="suggestions.json")

			logger.info(
				f"Successfully exported {len(suggestions)} suggestions in {format_type} format for admin {interaction.user.id}")
			await interaction.followup.send(
				f"📄 Exported {len(suggestions)} suggestions in {format_type} format:",
				file=file,
				ephemeral=True
			)

		except Exception as e:
			logger.error(f"Error during export for admin {interaction.user.id}: {e}", exc_info=True)
			await interaction.followup.send("❌ An error occurred during export.", ephemeral=True)


class SuggestionCog(commands.Cog):
	def __init__(self, bot):
		logger.info("Initializing SuggestionCog with new DatabaseManager")
		self.bot = bot
		self.suggestions_channel_id = 1371239792888516719
		self.admin_channel_id = 1265125349772230787

		# Initialize database connection using the new DatabaseManager
		try:
			self.db_manager = SuggestionDatabaseManager()
			logger.info("Successfully initialized suggestion database manager")
		except Exception as e:
			logger.error(f"Failed to initialize suggestion database manager: {e}", exc_info=True)
			raise

		# Add command groups
		self.suggestion_group = SuggestionCommandGroup(self)
		self.admin_group = SuggestionAdminGroup(self)

		# Start notification task
		self.notification_task.start()
		logger.info("SuggestionCog initialization completed")

	def cog_unload(self):
		logger.info("Unloading SuggestionCog")
		self.notification_task.cancel()

	@commands.Cog.listener()
	async def on_ready(self):
		"""Add persistent views when bot starts"""
		logger.info("Adding persistent views for suggestion system")
		# Initialize database manager on bot ready
		await self.db_manager._ensure_initialized()
		self.bot.add_view(SuggestionView("", self.db_manager))

	async def _process_suggestion(self, interaction: discord.Interaction,
								  suggestion_text: str, anonymous: bool, category: str):
		"""Process suggestion submission"""
		with log_context(logger, f"process_suggestion_{category}"):
			user = interaction.user
			logger.info(
				f"Processing suggestion from user {user.id} - Category: {category}, Anonymous: {anonymous}, Length: {len(suggestion_text)} chars")

			await interaction.response.defer(ephemeral=anonymous)

			if len(suggestion_text) > 2000:
				logger.warning(f"Suggestion from user {user.id} rejected - too long ({len(suggestion_text)} chars)")
				await interaction.followup.send(
					"❌ Your suggestion is too long. Please keep it under 2000 characters.",
					ephemeral=True
				)
				return

			# Check for similar suggestions
			similar_suggestions = await self.db_manager.search_suggestions(suggestion_text[:50], limit=3)
			if similar_suggestions:
				logger.info(f"Found {len(similar_suggestions)} similar suggestions for user {user.id}'s submission")
				similar_list = "\n".join([f"• {s['text'][:100]}..." for s in similar_suggestions[:3]])
				embed = discord.Embed(
					title="⚠️ Similar Suggestions Found",
					description=f"Found {len(similar_suggestions)} similar suggestions:\n\n{similar_list}",
					color=discord.Color.orange()
				)
				view = discord.ui.View()

				async def continue_anyway(interaction_inner):
					logger.info(f"User {user.id} chose to submit despite similar suggestions")
					await interaction_inner.response.defer()
					await self._create_suggestion_post(interaction, suggestion_text, anonymous, category)

				async def cancel_suggestion(interaction_inner):
					logger.info(f"User {user.id} cancelled submission after seeing similar suggestions")
					await interaction_inner.response.send_message("✅ Suggestion cancelled.", ephemeral=True)

				continue_btn = discord.ui.Button(label="Submit Anyway",
												 style=cast(discord.ButtonStyle, discord.ButtonStyle.success))
				cancel_btn = discord.ui.Button(label="Cancel",
											   style=cast(discord.ButtonStyle, discord.ButtonStyle.danger))
				continue_btn.callback = continue_anyway
				cancel_btn.callback = cancel_suggestion

				view.add_item(continue_btn)
				view.add_item(cancel_btn)

				await interaction.followup.send(embed=embed, view=view, ephemeral=True)
			else:
				logger.info(f"No similar suggestions found for user {user.id}'s submission, proceeding directly")
				await self._create_suggestion_post(interaction, suggestion_text, anonymous, category)

	async def _create_suggestion_post(self, interaction: discord.Interaction,
									  suggestion_text: str, anonymous: bool, category: str):
		"""Create the actual suggestion post"""
		with PerformanceLogger(logger, "create_suggestion_post"):
			user = interaction.user
			logger.info(f"Creating suggestion post for user {user.id} - Category: {category}, Anonymous: {anonymous}")

			try:
				# Create suggestion in database
				suggestion_id = await self.db_manager.create_suggestion(
					user.id, suggestion_text, anonymous, category
				)

				# Prepare embeds
				status_colors = {
					"Pending": discord.Color.blue(),
					"Under Review": discord.Color.orange(),
					"Approved": discord.Color.green(),
					"Implemented": discord.Color.gold(),
					"Rejected": discord.Color.red(),
					"On Hold": discord.Color.purple()
				}

				public_embed = discord.Embed(
					title="📬 New Suggestion",
					description=suggestion_text,
					color=status_colors.get("Pending", discord.Color.blue()),
					timestamp=interaction.created_at,
				)

				public_embed.add_field(name="Category", value=category, inline=True)
				public_embed.add_field(name="Status", value="Pending", inline=True)
				public_embed.add_field(name="ID", value=suggestion_id[:8], inline=True)
				public_embed.add_field(name="Votes", value="👍 0 | 👎 0 | ❤️ 0 | 🤔 0", inline=False)

				if anonymous:
					public_embed.set_author(name="Anonymous")
					public_embed.set_footer(text="Submitted anonymously")
				else:
					user_avatar_url = user.avatar.url if user.avatar else None
					public_embed.set_author(name=user.display_name, icon_url=user_avatar_url)
					public_embed.set_footer(text=f"Suggested by {user}", icon_url=user_avatar_url)

				# Admin embed
				admin_embed = discord.Embed(
					title="📬 New Suggestion (Admin Copy)",
					description=suggestion_text,
					color=discord.Color.red(),
					timestamp=interaction.created_at,
				)

				admin_embed.add_field(name="Category", value=category, inline=True)
				admin_embed.add_field(name="Anonymous", value=str(anonymous), inline=True)
				admin_embed.add_field(name="Suggestion ID", value=suggestion_id, inline=False)

				user_avatar_url = user.avatar.url if user.avatar else None
				admin_embed.set_author(name=user.display_name, icon_url=user_avatar_url)
				admin_embed.add_field(name="User ID", value=f"{user.id}", inline=False)
				admin_embed.set_footer(text=f"Suggested by {user}", icon_url=user_avatar_url)

				# Get channels
				suggestions_channel = self.bot.get_channel(self.suggestions_channel_id)
				admin_channel = self.bot.get_channel(self.admin_channel_id)

				if not suggestions_channel:
					logger.error("Suggestions channel not found")
					await interaction.followup.send(
						"❌ Suggestions channel not available.",
						ephemeral=True
					)
					return

				# Create suggestion view
				view = SuggestionView(suggestion_id, self.db_manager)

				# Send to suggestions channel
				message = await suggestions_channel.send(
					content="Anonymous Suggestion:" if anonymous else f"Suggestion from {user.mention}:",
					embed=public_embed,
					view=view
				)
				logger.info(f"Posted suggestion {suggestion_id} to suggestions channel (message {message.id})")

				# Create thread
				thread = await message.create_thread(
					name=f"Discussion: {category} Suggestion"
					if anonymous
					else f"Discussion: {user.display_name}'s {category} Suggestion",
					auto_archive_duration=4320,
				)

				thread_message = await thread.send(
					content="Let's discuss this suggestion!"
					if anonymous
					else f"Let's discuss {user.mention}'s suggestion!"
				)
				logger.info(f"Created discussion thread {thread.id} for suggestion {suggestion_id}")

				# Update database with message and thread IDs
				await db_manager.suggestions_suggestions.update_one(
					{"suggestion_id": suggestion_id},
					{
						"$set": {
							"message_id": message.id,
							"thread_id": thread.id
						}
					}
				)
				logger.debug(f"Updated suggestion {suggestion_id} with message and thread IDs")

				# Send admin copy
				if admin_channel:
					await admin_channel.send(embed=admin_embed)
					logger.info(f"Sent admin copy of suggestion {suggestion_id} to admin channel")
				else:
					logger.warning("Admin channel not found, admin copy not sent")

				# Notify user
				success_message = f"✅ Your {'anonymous ' if anonymous else ''}suggestion has been posted! (ID: {suggestion_id[:8]})"

				if hasattr(interaction, 'followup'):
					await interaction.followup.send(success_message, ephemeral=True)
				else:
					await interaction.response.send_message(success_message, ephemeral=True)

				logger.info(f"Successfully processed suggestion {suggestion_id} for user {user.id}")

			except Exception as e:
				logger.error(f"Error creating suggestion post for user {user.id}: {e}", exc_info=True)

				error_message = "❌ An error occurred while processing your suggestion."
				if hasattr(interaction, 'followup'):
					await interaction.followup.send(error_message, ephemeral=True)
				else:
					await interaction.response.send_message(error_message, ephemeral=True)

	@tasks.loop(minutes=5)
	async def notification_task(self):
		"""Process pending notifications"""
		with log_context(logger, "notification_processing"):
			try:
				notifications = await self.db_manager.get_pending_notifications()

				if notifications:
					logger.info(f"Processing {len(notifications)} pending notifications")

				for notification in notifications:
					try:
						user = self.bot.get_user(notification["user_id"])
						if user:
							embed = discord.Embed(
								title="📬 Suggestion Update",
								color=discord.Color.blue()
							)

							embed.add_field(
								name="Suggestion ID",
								value=notification["suggestion_id"][:8],
								inline=True
							)

							embed.add_field(
								name="New Status",
								value=notification["status"],
								inline=True
							)

							if notification.get("reason"):
								embed.add_field(
									name="Reason",
									value=notification["reason"],
									inline=False
								)

							try:
								await user.send(embed=embed)
								await self.db_manager.mark_notification_sent(notification["_id"])
								logger.info(
									f"Sent notification to user {notification['user_id']} for suggestion {notification['suggestion_id']}")
							except discord.Forbidden:
								# User has DMs disabled, mark as sent anyway
								await self.db_manager.mark_notification_sent(notification["_id"])
								logger.warning(
									f"Could not send DM to user {notification['user_id']} (DMs disabled), marking as sent")
						else:
							logger.warning(f"User {notification['user_id']} not found for notification")

					except Exception as e:
						logger.error(f"Error sending notification {notification.get('_id')}: {e}", exc_info=True)
						continue

			except Exception as e:
				logger.error(f"Error in notification task: {e}", exc_info=True)

	@notification_task.before_loop
	async def before_notification_task(self):
		logger.info("Waiting for bot to be ready before starting notification task")
		await self.bot.wait_until_ready()
		logger.info("Bot ready, notification task can now start")

	# Error handlers for group commands
	async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
		if isinstance(error, app_commands.errors.CommandOnCooldown):
			logger.info(f"User {interaction.user.id} hit cooldown on suggestion command")
			await interaction.response.send_message(
				f"⏳ You're on cooldown! Please try again in {int(error.retry_after)} seconds.",
				ephemeral=True
			)
		else:
			logger.error(f"Unhandled error in suggestion command for user {interaction.user.id}: {error}",
						 exc_info=True)
			if not interaction.response.is_done():
				await interaction.response.send_message(
					"❌ An unexpected error occurred. Please try again later.",
					ephemeral=True
				)


async def setup(bot: commands.Bot):
	"""Load the Cog"""
	logger.info("Setting up SuggestionCog")
	try:
		cog = SuggestionCog(bot)
		await bot.add_cog(cog)
		# Add the command groups to the tree
		bot.tree.add_command(cog.suggestion_group)
		bot.tree.add_command(cog.admin_group)
		logger.info("SuggestionCog setup completed successfully")
	except Exception as e:
		logger.error(f"Failed to set up SuggestionCog: {e}", exc_info=True)
		raise