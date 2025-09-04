import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional

from NewMembers.joining import guild_handler
from utils.logger import get_logger
from utils.cooldown import welcome_cooldown

logger = get_logger("WelcomeTrigger")

WELCOME_CHANNEL_ID = 1371686628510269460


def has_welcome_permissions_app():
    """App command check for welcome message permissions."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False

        user = interaction.user
        perms = getattr(user, "guild_permissions", None)
        if perms and (perms.manage_messages or perms.administrator):
            return True

        role_names = [role.name.lower() for role in getattr(user, "roles", [])]
        allowed_roles = {'admin', 'moderator', 'welcome manager', 'staff'}
        return any(name in allowed_roles for name in role_names)

    return app_commands.check(predicate)


class WelcomeGroup(commands.GroupCog, name="welcome", description="Welcome system commands"):
    """
    Group cog providing:
    - /welcome test [member]
    - /welcome info
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.handler = guild_handler
        logger.info("WelcomeGroup initialized")

    @app_commands.command(name="test", description="Test the welcome message system for a member (default: you)")
    @app_commands.describe(member="Member to test the welcome message for")
    @has_welcome_permissions_app()
    @app_commands.guild_only()
    @app_commands.check(welcome_cooldown())
    async def test(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        """Slash command to test welcome message."""
        target_member = member or interaction.user

        # Quick confirmation (ephemeral)
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.InteractionResponded:
            pass

        try:
            # Perform the welcome action
            await self.handler.send_welcome_message(target_member)

            # Log the action
            logger.info(
                f"Welcome test triggered by {interaction.user} ({interaction.user.id}) "
                f"for {target_member} ({target_member.id})"
            )

            # Success confirmation
            embed = discord.Embed(
                title="✅ Welcome Message Test Complete",
                description=f"Welcome message sent for {target_member.mention}",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text=f"Tested by {interaction.user}", icon_url=interaction.user.display_avatar.url)

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Error during welcome message test: {e}", exc_info=True)

            error_embed = discord.Embed(
                title="❌ Welcome Message Test Failed",
                description=f"An error occurred while testing the welcome message:\n```{str(e)[:1000]}```",
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow()
            )
            error_embed.set_footer(text=f"Tested by {interaction.user}", icon_url=interaction.user.display_avatar.url)

            if interaction.followup:
                await interaction.followup.send(embed=error_embed, ephemeral=True)

    @app_commands.command(name="info", description="Show information about the welcome system")
    @has_welcome_permissions_app()
    @app_commands.guild_only()
    async def info(self, interaction: discord.Interaction):
        """Slash command to show welcome system information."""
        embed = discord.Embed(
            title="🔧 Welcome System Information",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )

        # Get welcome channel
        welcome_channel = self.bot.get_channel(WELCOME_CHANNEL_ID)

        embed.add_field(
            name="Welcome Channel",
            value=welcome_channel.mention if isinstance(welcome_channel, (discord.TextChannel, discord.Thread)) else "❌ Not found",
            inline=True
        )

        embed.add_field(
            name="Account Age Requirement",
            value="60 days",
            inline=True
        )

        embed.add_field(
            name="Commands Available",
            value="`/welcome test` - Test welcome message\n`/welcome info` - Show this info",
            inline=False
        )

        embed.add_field(
            name="Required Permissions",
            value="• `Manage Messages`\n• Administrator permission\n• Admin/Moderator/Staff/Welcome Manager role",
            inline=False
        )

        embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.display_avatar.url)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """Unified error handler for the welcome command group."""
        try:
            if isinstance(error, app_commands.CheckFailure):
                embed = discord.Embed(
                    title="❌ Permission Denied",
                    description="You don't have permission to use welcome commands.\nRequired: `Manage Messages` or Admin/Moderator/Staff/Welcome Manager role.",
                    color=discord.Color.red()
                )
                if interaction.response.is_done():
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    await interaction.response.send_message(embed=embed, ephemeral=True)

            elif isinstance(error, app_commands.CommandOnCooldown):
                embed = discord.Embed(
                    title="⏳ Cooldown Active",
                    description=f"Please wait {int(getattr(error, 'retry_after', 10))} seconds before trying again.",
                    color=discord.Color.orange()
                )
                if interaction.response.is_done():
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    await interaction.response.send_message(embed=embed, ephemeral=True)

            elif isinstance(error, app_commands.NoPrivateMessage):
                if interaction.response.is_done():
                    await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
                else:
                    await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)

            else:
                logger.error(f"Unhandled error in welcome commands: {error}", exc_info=True)
                embed = discord.Embed(
                    title="❌ Unexpected Error",
                    description="An unexpected error occurred. Please try again later.",
                    color=discord.Color.red()
                )
                if interaction.response.is_done():
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as inner_e:
            logger.error(f"Error in welcome cog error handler: {inner_e}", exc_info=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeGroup(bot))