import logging

import discord
from discord.ext import commands

from utils.logger import get_logger, log_context, log_performance

logger = get_logger("CloneEmbed")


class CloneEmbedCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info(f"CloneEmbedCog initialized for bot: {bot.user}")

    def has_clone_permissions(self):
        """Custom check for clone permissions"""

        async def predicate(ctx):
            # Check for specific roles or higher permissions
            has_perms = (
                    ctx.author.guild_permissions.manage_messages or
                    any(role.name.lower() in ['admin', 'moderator', 'embed manager'] for role in ctx.author.roles)
            )
            logger.debug(f"Permission check for {ctx.author} in {ctx.guild}: {has_perms}")
            return has_perms

        return commands.check(predicate)

    @commands.group(name="embed", invoke_without_command=True)
    @commands.has_permissions(manage_messages=True)
    async def embed_commands(self, ctx):
        """Embed management commands."""
        logger.info(f"Embed help command invoked by {ctx.author} in {ctx.guild}")

        embed = discord.Embed(
            title="🔧 Embed Management Commands",
            description="Available commands for managing embeds:",
            color=discord.Color.blue()
        )
        embed.add_field(
            name="📋 embed clone <message_link> [channel] [keep_original]",
            value="Clone an embed to current or specified channel",
            inline=False
        )
        embed.add_field(
            name="👀 embed preview <message_link>",
            value="Preview embed information before cloning",
            inline=False
        )
        embed.add_field(
            name="📦 embed batch <link1> <link2> ...",
            value="Clone multiple embeds at once (max 10)",
            inline=False
        )
        embed.add_field(
            name="⚙️ embed config [setting] [value]",
            value="View or modify embed settings (Admin only)",
            inline=False
        )
        embed.set_footer(text="Use embed <command> --help for detailed information")
        await ctx.send(embed=embed)
        logger.debug("Embed help menu sent successfully")

    @embed_commands.command(name="clone")
    @commands.has_permissions(manage_messages=True)
    @commands.cooldown(1, 30, commands.BucketType.user)
    @log_performance("embed_clone_operation")
    async def clone_embed(self, ctx, message_link: str, destination_channel: discord.TextChannel = None,
                          keep_original: bool = False):
        """
        Clones an embed from the provided message link and reposts it.

        :param ctx: The context of the command.
        :param message_link: The full message URL (must include channel_id and message_id).
        :param destination_channel: Optional channel to send the cloned embed to.
        :param keep_original: If True, doesn't delete the original message.
        """
        operation_id = f"clone_{ctx.author.id}_{hash(message_link) % 10000}"

        with log_context(logger, f"Clone embed operation [{operation_id}]"):
            try:
                logger.info(f"Clone embed command initiated - User: {ctx.author} ({ctx.author.id}), "
                            f"Guild: {ctx.guild} ({ctx.guild.id}), Link: {message_link[:50]}...")

                # Parse the message link and retrieve channel_id and message_id
                with log_context(logger, "Parsing message link", logging.DEBUG):
                    parts = message_link.strip().split("/")
                    if len(parts) < 2:
                        raise ValueError("Invalid link format - insufficient parts")

                    channel_id = int(parts[-2])
                    message_id = int(parts[-1])
                    logger.debug(f"Parsed IDs - Channel: {channel_id}, Message: {message_id}")

                # Fetch the message
                with log_context(logger, "Fetching source message and channel"):
                    channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                    if not channel:
                        raise discord.NotFound("Channel not found or inaccessible")

                    logger.debug(f"Source channel accessed - Name: {channel.name}, ID: {channel.id}, "
                                 f"Type: {type(channel).__name__}")

                    message = await channel.fetch_message(message_id)
                    logger.debug(f"Source message fetched - ID: {message.id}, Author: {message.author}, "
                                 f"Created: {message.created_at}, Embeds: {len(message.embeds)}")

                # Validate message content
                with log_context(logger, "Validating embed content"):
                    if not message.embeds:
                        logger.warning(f"No embeds found in message {message_id} from channel {channel.name}")
                        await ctx.send("❌ The message does not contain any embeds.")
                        return

                    if not message.author.bot:
                        logger.warning(f"Message {message_id} was not sent by a bot (author: {message.author})")
                        await ctx.send("❌ The message was not sent by a bot.")
                        return

                    logger.info(f"Validation passed - {len(message.embeds)} embed(s) found from bot {message.author}")

                # Determine target channel
                target_channel = destination_channel or channel
                logger.info(f"Target channel determined - Name: {target_channel.name}, ID: {target_channel.id}")

                # Clone all embeds
                if len(message.embeds) > 1:
                    logger.info(f"Multiple embeds detected ({len(message.embeds)}), notifying user")
                    await ctx.send(f"📋 Found {len(message.embeds)} embeds. Cloning all to {target_channel.mention}...")

                with log_context(logger, f"Cloning {len(message.embeds)} embed(s)"):
                    cloned_messages = []
                    for i, embed in enumerate(message.embeds):
                        try:
                            cloned_msg = await target_channel.send(embed=embed)
                            cloned_messages.append(cloned_msg.id)
                            logger.debug(f"Embed {i + 1}/{len(message.embeds)} cloned successfully - "
                                         f"New message ID: {cloned_msg.id}")
                        except Exception as embed_error:
                            logger.error(f"Failed to clone embed {i + 1}/{len(message.embeds)}: {embed_error}")
                            raise

                    logger.info(f"All embeds cloned successfully - New message IDs: {cloned_messages}")

                # Enhanced audit logging
                audit_info = {
                    'operation_id': operation_id,
                    'action': 'embed_clone',
                    'executor': {
                        'name': str(ctx.author),
                        'id': ctx.author.id,
                        'discriminator': ctx.author.discriminator,
                        'guild_permissions': [perm for perm, value in ctx.author.guild_permissions if value]
                    },
                    'source': {
                        'message_id': message_id,
                        'channel_name': channel.name,
                        'channel_id': channel.id,
                        'author': str(message.author),
                        'author_id': message.author.id,
                        'created_at': message.created_at.isoformat()
                    },
                    'target': {
                        'channel_name': target_channel.name,
                        'channel_id': target_channel.id,
                        'new_message_ids': cloned_messages
                    },
                    'metadata': {
                        'embed_count': len(message.embeds),
                        'keep_original': keep_original,
                        'timestamp': discord.utils.utcnow().isoformat(),
                        'guild_id': ctx.guild.id,
                        'guild_name': ctx.guild.name
                    }
                }

                # Handle original message deletion
                with log_context(logger, "Processing original message"):
                    if not keep_original:
                        try:
                            await message.delete()
                            audit_info['metadata']['original_deleted'] = True
                            logger.info(f"Original message {message_id} deleted successfully")
                        except discord.Forbidden as e:
                            logger.error(
                                f"Failed to delete original message {message_id} - insufficient permissions: {e}")
                            audit_info['metadata']['original_deleted'] = False
                            audit_info['metadata']['deletion_error'] = 'insufficient_permissions'
                            await ctx.send(
                                "⚠️ Embeds cloned successfully, but I couldn't delete the original message (insufficient permissions).")
                        except Exception as e:
                            logger.error(f"Failed to delete original message {message_id}: {e}")
                            audit_info['metadata']['original_deleted'] = False
                            audit_info['metadata']['deletion_error'] = str(e)
                    else:
                        audit_info['metadata']['original_deleted'] = False
                        logger.info("Original message preserved as requested")

                # Log comprehensive audit trail
                logger.info(f"Embed clone audit trail: {audit_info}")

                # Send success confirmation
                success_msg = f"✅ {len(message.embeds)} embed(s) cloned to {target_channel.mention}"
                if not keep_original and audit_info['metadata'].get('original_deleted', False):
                    success_msg += " and original message deleted"
                success_msg += " successfully."

                await ctx.send(success_msg)
                logger.info(f"Clone operation completed successfully for user {ctx.author}")

            except (IndexError, ValueError) as e:
                error_msg = f"Invalid message link format provided by {ctx.author}: {message_link}"
                logger.error(f"{error_msg} - Error: {e}")
                await ctx.send("❌ Invalid message link format. Make sure it includes both channel ID and message ID.")

            except discord.Forbidden as e:
                error_msg = f"Permission denied for {ctx.author} accessing channel or deleting message"
                logger.error(f"{error_msg} - Error: {e}")
                await ctx.send("❌ I do not have permission to access the specified channel or delete the message.")

            except discord.NotFound as e:
                error_msg = f"Resource not found for link: {message_link}"
                logger.error(f"{error_msg} - Error: {e}")
                await ctx.send("❌ The specified message or channel was not found.")

            except Exception as e:
                error_msg = f"Unexpected error in clone_embed operation for user {ctx.author}"
                logger.exception(f"{error_msg}: {e}")
                await ctx.send(f"❌ An unexpected error occurred: {str(e)[:100]}...")

    @embed_commands.command(name="preview")
    @commands.has_permissions(manage_messages=True)
    @log_performance("embed_preview_operation")
    async def preview_embed(self, ctx, message_link: str):
        """Preview an embed before cloning it."""
        with log_context(logger, f"Preview embed operation for {ctx.author}"):
            try:
                logger.info(f"Preview command initiated - User: {ctx.author} ({ctx.author.id}), "
                            f"Guild: {ctx.guild}, Link: {message_link[:50]}...")

                # Parse the message link and retrieve channel_id and message_id
                with log_context(logger, "Parsing and fetching preview content"):
                    parts = message_link.strip().split("/")
                    if len(parts) < 2:
                        raise ValueError("Invalid link format")

                    channel_id = int(parts[-2])
                    message_id = int(parts[-1])
                    logger.debug(f"Preview target - Channel: {channel_id}, Message: {message_id}")

                    # Fetch the message
                    channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                    message = await channel.fetch_message(message_id)

                    logger.debug(f"Preview source - Channel: {channel.name}, Message author: {message.author}, "
                                 f"Embeds: {len(message.embeds)}")

                if not message.embeds or not message.author.bot:
                    logger.warning(f"Preview failed - No valid embeds in message {message_id}")
                    await ctx.send("❌ The message does not contain any embeds or was not sent by a bot.")
                    return

                # Create preview embed
                with log_context(logger, "Building preview embed"):
                    preview_embed = discord.Embed(
                        title="📋 Embed Preview",
                        color=discord.Color.blue()
                    )

                    preview_embed.add_field(
                        name="📍 Source Info",
                        value=f"**Channel:** {channel.mention}\n"
                              f"**Author:** {message.author.mention}\n"
                              f"**Created:** {message.created_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
                              f"**Total Embeds:** {len(message.embeds)}",
                        inline=False
                    )

                    for i, embed in enumerate(message.embeds):
                        field_value = f"• **Title:** {embed.title or 'None'}\n"
                        field_value += f"• **Description:** {len(embed.description) if embed.description else 0} chars\n"
                        field_value += f"• **Fields:** {len(embed.fields)}\n"
                        field_value += f"• **Color:** {embed.color}\n"
                        field_value += f"• **Image:** {'Yes' if embed.image else 'No'}\n"
                        field_value += f"• **Thumbnail:** {'Yes' if embed.thumbnail else 'No'}"

                        preview_embed.add_field(
                            name=f"Embed {i + 1}",
                            value=field_value,
                            inline=True
                        )

                    logger.debug(f"Preview embed created with {len(message.embeds)} embed summaries")

                await ctx.send(embed=preview_embed)
                logger.info(f"Preview sent successfully to {ctx.author}")

            except (IndexError, ValueError) as e:
                logger.error(f"Invalid preview link from {ctx.author}: {message_link} - {e}")
                await ctx.send("❌ Invalid message link format. Make sure it includes both channel ID and message ID.")
            except Exception as e:
                logger.exception(f"Error in preview operation for {ctx.author}: {e}")
                await ctx.send(f"❌ Error previewing embed: {str(e)[:100]}...")

    @embed_commands.command(name="batch")
    @commands.has_permissions(manage_messages=True)
    @commands.cooldown(1, 60, commands.BucketType.user)
    @log_performance("batch_clone_operation")
    async def batch_clone(self, ctx, *message_links):
        """Clone multiple embeds from different messages."""
        batch_id = f"batch_{ctx.author.id}_{hash(str(message_links)) % 10000}"

        with log_context(logger, f"Batch clone operation [{batch_id}]"):
            logger.info(f"Batch clone initiated - User: {ctx.author} ({ctx.author.id}), "
                        f"Guild: {ctx.guild}, Links: {len(message_links)}")

            if not message_links:
                logger.warning(f"Batch clone called with no links by {ctx.author}")
                await ctx.send("❌ Please provide at least one message link.")
                return

            if len(message_links) > 10:  # Safety limit
                logger.warning(f"Batch clone limit exceeded by {ctx.author}: {len(message_links)} links")
                await ctx.send("❌ Maximum 10 messages can be cloned at once.")
                return

            successful_clones = 0
            failed_clones = 0
            total_embeds = 0
            batch_results = []

            status_msg = await ctx.send(f"🔄 Starting batch clone of {len(message_links)} messages...")
            logger.info(f"Batch processing started for {len(message_links)} messages")

            for i, link in enumerate(message_links):
                link_result = {
                    'index': i + 1,
                    'link': link[:50] + '...' if len(link) > 50 else link,
                    'success': False,
                    'embeds_count': 0,
                    'error': None
                }

                try:
                    with log_context(logger, f"Processing batch item {i + 1}/{len(message_links)}", logging.DEBUG):
                        # Parse and fetch message
                        parts = link.strip().split("/")
                        channel_id = int(parts[-2])
                        message_id = int(parts[-1])

                        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                        message = await channel.fetch_message(message_id)

                        if message.embeds and message.author.bot:
                            for embed in message.embeds:
                                await channel.send(embed=embed)
                                total_embeds += 1

                            await message.delete()
                            successful_clones += 1
                            link_result['success'] = True
                            link_result['embeds_count'] = len(message.embeds)

                            logger.debug(
                                f"Batch item {i + 1} successful: {len(message.embeds)} embeds from {link[:30]}...")
                        else:
                            failed_clones += 1
                            link_result['error'] = 'No valid embeds or not from bot'
                            logger.warning(f"Batch item {i + 1} failed: No valid embeds in {link[:30]}...")

                except Exception as e:
                    failed_clones += 1
                    link_result['error'] = str(e)[:100]
                    logger.error(f"Batch item {i + 1} failed: {e} - Link: {link[:30]}...")

                batch_results.append(link_result)

            # Log comprehensive batch results
            batch_audit = {
                'batch_id': batch_id,
                'executor': str(ctx.author),
                'executor_id': ctx.author.id,
                'guild_id': ctx.guild.id,
                'total_links': len(message_links),
                'successful_clones': successful_clones,
                'failed_clones': failed_clones,
                'total_embeds_cloned': total_embeds,
                'results': batch_results,
                'timestamp': discord.utils.utcnow().isoformat()
            }

            logger.info(f"Batch clone audit: {batch_audit}")

            # Update status message
            result_embed = discord.Embed(
                title="📦 Batch Clone Complete",
                color=discord.Color.green() if failed_clones == 0 else discord.Color.orange()
            )
            result_embed.add_field(name="✅ Successful", value=str(successful_clones), inline=True)
            result_embed.add_field(name="❌ Failed", value=str(failed_clones), inline=True)
            result_embed.add_field(name="📋 Total Embeds", value=str(total_embeds), inline=True)

            await status_msg.edit(content="", embed=result_embed)
            logger.info(f"Batch clone completed - Success: {successful_clones}, Failed: {failed_clones}, "
                        f"Total embeds: {total_embeds}")

    @embed_commands.command(name="config")
    @commands.has_permissions(administrator=True)
    async def embed_config(self, ctx, setting: str = None, *, value: str = None):
        """Configure embed cloning settings for this server."""
        with log_context(logger, f"Config command by {ctx.author}"):
            logger.info(f"Config command accessed by {ctx.author} ({ctx.author.id}) in {ctx.guild}")

            # Default settings (you can store these in a database)
            default_settings = {
                'max_batch_size': 10,
                'cooldown_seconds': 30,
                'allowed_roles': ['admin', 'moderator', 'embed manager'],
                'auto_delete_original': True,
                'log_channel': None
            }

            if not setting:
                # Show current settings
                logger.debug("Displaying all config settings")
                config_embed = discord.Embed(
                    title="⚙️ Current Embed Settings",
                    description="Configure how embed cloning works in this server",
                    color=discord.Color.blue()
                )

                for key, val in default_settings.items():
                    if isinstance(val, list):
                        val = ', '.join(val)
                    config_embed.add_field(name=key.replace('_', ' ').title(), value=str(val), inline=True)

                config_embed.set_footer(text="Use: embed config <setting> <value> to modify")
                await ctx.send(embed=config_embed)

            elif setting and not value:
                # Show specific setting
                logger.debug(f"Displaying specific setting: {setting}")
                if setting in default_settings:
                    val = default_settings[setting]
                    if isinstance(val, list):
                        val = ', '.join(val)
                    await ctx.send(f"**{setting.replace('_', ' ').title()}:** {val}")
                else:
                    logger.warning(f"Unknown setting requested: {setting}")
                    await ctx.send(f"❌ Setting '{setting}' not found.")

            else:
                # Modify setting (implementation depends on your storage system)
                logger.info(f"Config modification requested - Setting: {setting}, Value: {value}")
                await ctx.send(f"⚙️ Setting '{setting}' would be updated to '{value}' (Database integration required)")


# Cog setup function
async def setup(bot: commands.Bot):
    logger.info("Setting up CloneEmbedCog...")
    try:
        await bot.add_cog(CloneEmbedCog(bot))
        logger.info("CloneEmbedCog successfully added to bot")
    except Exception as e:
        logger.exception(f"Failed to setup CloneEmbedCog: {e}")
        raise