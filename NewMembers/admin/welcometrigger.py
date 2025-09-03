import discord
from discord.ext import commands
from NewMembers.joining import send_welcome_message
from utils.logger import get_logger

logger = get_logger("WelcomeTrigger")

def has_welcome_permissions():
    """Custom check for welcome message testing permissions"""
    async def predicate(ctx):
        # Check for specific roles or higher permissions
        return (
            ctx.author.guild_permissions.manage_messages or
            ctx.author.guild_permissions.administrator or
            any(role.name.lower() in ['admin', 'moderator', 'welcome manager', 'staff'] for role in ctx.author.roles)
        )
    return commands.check(predicate)

class WelcomeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="testwelcome", aliases=["test_welcome", "welcome_test"])
    @has_welcome_permissions()
    @commands.guild_only()
    @commands.cooldown(1, 10, commands.BucketType.user)  # 1 use per 10 seconds per user
    async def test_welcome(self, ctx, member: discord.Member = None):
        """
        Test the welcome message system by sending a welcome message for a specified member.
        If no member is specified, uses the command author.

        Usage:
        .testwelcome
        .testwelcome @user
        .testwelcome user_id
        """
        # Use the command author if no member is specified
        target_member = member or ctx.author

        try:
            # Send a confirmation message
            await ctx.send(f"🧪 Testing welcome message for {target_member.mention}...", delete_after=5)

            # Send the welcome message
            await send_welcome_message(target_member)

            # Log the action
            logger.info(
                f"Welcome message test triggered by {ctx.author} ({ctx.author.id}) for {target_member} ({target_member.id})")

            # Send success confirmation
            embed = discord.Embed(
                title="✅ Welcome Message Test Complete",
                description=f"Welcome message sent for {target_member.mention}",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )
            embed.set_footer(text=f"Tested by {ctx.author}", icon_url=ctx.author.display_avatar.url)

            await ctx.send(embed=embed, delete_after=15)

        except Exception as e:
            logger.error(f"Error during welcome message test: {e}")

            error_embed = discord.Embed(
                title="❌ Welcome Message Test Failed",
                description=f"An error occurred while testing the welcome message:\n```{str(e)[:1000]}```",
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow()
            )
            error_embed.set_footer(text=f"Tested by {ctx.author}", icon_url=ctx.author.display_avatar.url)

            await ctx.send(embed=error_embed, delete_after=30)

    @test_welcome.error
    async def test_welcome_error(self, ctx, error):
        """Handle errors for the test_welcome command"""
        if isinstance(error, commands.CheckFailure):
            embed = discord.Embed(
                title="❌ Permission Denied",
                description="You don't have permission to test welcome messages.\nRequired: `Manage Messages` permission or Admin/Moderator/Staff role.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed, delete_after=10)

        elif isinstance(error, commands.CommandOnCooldown):
            embed = discord.Embed(
                title="⏳ Cooldown Active",
                description=f"Please wait {int(error.retry_after)} seconds before testing again.",
                color=discord.Color.orange()
            )
            await ctx.send(embed=embed, delete_after=5)

        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("❌ This command can only be used in a server.", delete_after=5)

        elif isinstance(error, commands.MemberNotFound):
            embed = discord.Embed(
                title="❌ Member Not Found",
                description="I couldn't find that member. Please mention them or use their ID.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed, delete_after=10)

        else:
            logger.error(f"Unhandled error in test_welcome command: {error}")
            embed = discord.Embed(
                title="❌ Unexpected Error",
                description="An unexpected error occurred. Please try again later.",
                color=discord.Color.red()
            )
            await ctx.send(embed=embed, delete_after=10)

    @commands.command(name="welcomeinfo", aliases=["welcome_info"])
    @has_welcome_permissions()
    @commands.guild_only()
    async def welcome_info(self, ctx):
        """Show information about the welcome system"""
        embed = discord.Embed(
            title="🔧 Welcome System Information",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )

        # Get welcome channel
        welcome_channel = self.bot.get_channel(1371686628510269460)  # WELCOME_CHANNEL_ID

        embed.add_field(
            name="Welcome Channel",
            value=welcome_channel.mention if welcome_channel else "❌ Not found",
            inline=True
        )

        embed.add_field(
            name="Account Age Requirement",
            value="60 days",
            inline=True
        )

        embed.add_field(
            name="Commands Available",
            value="`.testwelcome` - Test welcome message\n`.welcomeinfo` - Show this info",
            inline=False
        )

        embed.add_field(
            name="Required Permissions",
            value="• `Manage Messages`\n• Administrator role\n• Admin/Moderator/Staff/Welcome Manager role",
            inline=False
        )

        embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)

        await ctx.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeCog(bot))