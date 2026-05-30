from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..db import Database
from ..openrouter import OpenRouterOCRClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProfileSubmission:
    discord_user_id: int
    source_channel_id: int
    source_message_id: int
    attachment_url: str
    ocr_text: str
    player_name: str


class ProfileReviewView(discord.ui.View):
    def __init__(self, cog: "ProfileCog", submission_id: int, requester_id: int, player_name: str, ocr_text: str) -> None:
        super().__init__(timeout=3600)
        self.cog = cog
        self.submission_id = submission_id
        self.requester_id = requester_id
        self.player_name = player_name
        self.ocr_text = ocr_text

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("This confirmation is not for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handle_profile_decision(interaction, self.submission_id, self.player_name, approved=True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handle_profile_decision(interaction, self.submission_id, self.player_name, approved=False)


class ProfileCog(commands.Cog):
    def __init__(self, bot: commands.Bot, database: Database, settings) -> None:
        self.bot = bot
        self.database = database
        self.settings = settings
        self.ocr_client = None
        if settings.openrouter_api_key:
            self.ocr_client = OpenRouterOCRClient(
                api_key=settings.openrouter_api_key,
                model=settings.openrouter_model,
                base_url=settings.openrouter_base_url,
                app_name=settings.openrouter_app_name,
                site_url=settings.openrouter_site_url,
            )

    profile = app_commands.Group(name="profile", description="Profile submission workflow")

    @profile.command(name="announce", description="Post the profile submission instructions in the submission channel")
    async def announce(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        channel = await self._get_submission_channel()
        if channel is None:
            await interaction.followup.send("PROFILE_SUBMISSION_CHANNEL_ID is not configured.", ephemeral=True)
            return

        await self.refresh_profile_announcement(channel=channel)
        await interaction.followup.send(f"Posted instructions in {channel.mention}.", ephemeral=True)

    async def refresh_profile_announcement(self, channel: discord.TextChannel | None = None) -> None:
        if self.settings.profile_submission_channel_id is None:
            return

        resolved_channel = channel or await self._get_submission_channel()
        if resolved_channel is None:
            return

        old_message_id = self._get_announcement_message_id(resolved_channel.id)
        if old_message_id is not None:
            with suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
                old_message = await resolved_channel.fetch_message(old_message_id)
                await old_message.delete()

        await self._delete_previous_bot_messages(resolved_channel)

        embed = discord.Embed(
            title="Submit Your Profile",
            description=(
                "Send a screenshot of your profile in this channel. "
                "The bot will OCR your player name, then DM you a private confirmation to approve or decline it."
            ),
            color=discord.Color.teal(),
        )
        embed.set_footer(text="New bot restart = new pinned instructions post")

        new_message = await resolved_channel.send(embed=embed)
        self._save_announcement_message_id(resolved_channel.id, new_message.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        if self.settings.profile_submission_channel_id is None:
            return

        if message.channel.id != self.settings.profile_submission_channel_id:
            return

        image_attachment = self._first_image_attachment(message.attachments)
        if image_attachment is None:
            return

        if self.ocr_client is None:
            await self._safe_notify_user(message.author, "Profile OCR is not configured yet. Please contact an admin.")
            return

        try:
            image_bytes = await image_attachment.read()
            extracted_name = await self.ocr_client.extract_player_name(image_bytes, image_attachment.content_type or "image/png")
            normalized_name = self._normalize_name(extracted_name)
            if not normalized_name:
                raise RuntimeError("OCR returned an empty player name")

            submission_id = self._save_submission(
                discord_user_id=message.author.id,
                source_channel_id=message.channel.id,
                source_message_id=message.id,
                attachment_url=image_attachment.url,
                ocr_text=extracted_name,
                player_name=normalized_name,
            )

            dm_channel = await message.author.create_dm()
            embed = discord.Embed(
                title="Confirm Your Profile Name",
                description="Review the OCR result below and approve or decline it.",
                color=discord.Color.blurple(),
            )
            embed.add_field(name="Extracted Name", value=normalized_name, inline=False)
            embed.add_field(name="Raw OCR", value=extracted_name[:1024], inline=False)
            embed.set_image(url=image_attachment.url)

            view = ProfileReviewView(self, submission_id, message.author.id, normalized_name, extracted_name)
            await dm_channel.send(embed=embed, view=view)

        except Exception as exc:
            logger.exception("Failed to process profile screenshot from %s", message.author.id)
            await self._safe_notify_user(message.author, f"I could not read your screenshot: {exc}")

    async def handle_profile_decision(
        self,
        interaction: discord.Interaction,
        submission_id: int,
        player_name: str,
        approved: bool,
    ) -> None:
        with self.database.connect() as connection:
            with connection.cursor() as cursor:
                if approved:
                    cursor.execute(
                        """
                        INSERT INTO player_stats (player_name)
                        VALUES (%s)
                        ON CONFLICT (player_name) DO NOTHING
                        """,
                        (player_name,),
                    )

                cursor.execute(
                    """
                    UPDATE profile_submissions
                    SET status = %s,
                        reviewed_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                      AND status = 'pending'
                    RETURNING id
                    """,
                    ("approved" if approved else "declined", submission_id),
                )
                updated = cursor.fetchone()

        if updated is None:
            await interaction.response.send_message("This submission was already handled.", ephemeral=True)
            return

        message_text = (
            f"Approved. {player_name} is now registered."
            if approved
            else f"Declined. {player_name} was not registered."
        )
        await interaction.response.edit_message(content=message_text, embed=None, view=None)

    def _save_submission(
        self,
        discord_user_id: int,
        source_channel_id: int,
        source_message_id: int,
        attachment_url: str,
        ocr_text: str,
        player_name: str,
    ) -> int:
        with self.database.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO profile_submissions (
                        discord_user_id,
                        source_channel_id,
                        source_message_id,
                        attachment_url,
                        ocr_text,
                        player_name,
                        status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 'pending')
                    RETURNING id
                    """,
                    (discord_user_id, source_channel_id, source_message_id, attachment_url, ocr_text, player_name),
                )
                row = cursor.fetchone()

        return int(row["id"])

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if getattr(self.bot, "_profile_announcement_refreshed", False):
            return

        setattr(self.bot, "_profile_announcement_refreshed", True)
        await self.refresh_profile_announcement()

    async def _get_submission_channel(self) -> discord.TextChannel | None:
        if self.settings.profile_submission_channel_id is None:
            return None

        channel = self.bot.get_channel(self.settings.profile_submission_channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel

        fetched_channel = await self.bot.fetch_channel(self.settings.profile_submission_channel_id)
        if isinstance(fetched_channel, discord.TextChannel):
            return fetched_channel

        return None

    def _get_announcement_message_id(self, channel_id: int) -> int | None:
        with self.database.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT message_id FROM profile_announcement_state WHERE channel_id = %s",
                    (channel_id,),
                )
                row = cursor.fetchone()

        return int(row["message_id"]) if row else None

    def _save_announcement_message_id(self, channel_id: int, message_id: int) -> None:
        with self.database.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO profile_announcement_state (channel_id, message_id, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (channel_id) DO UPDATE
                    SET message_id = EXCLUDED.message_id,
                        updated_at = NOW()
                    """,
                    (channel_id, message_id),
                )

    async def _delete_previous_bot_messages(self, channel: discord.TextChannel) -> None:
        bot_user = self.bot.user
        if bot_user is None:
            return

        async for message in channel.history(limit=100):
            if message.author.id != bot_user.id:
                continue

            with suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
                await message.delete()

    @staticmethod
    async def _safe_notify_user(user: discord.User | discord.Member, message: str) -> None:
        try:
            await user.send(message)
        except discord.Forbidden:
            return

    @staticmethod
    def _first_image_attachment(attachments: list[discord.Attachment]) -> discord.Attachment | None:
        for attachment in attachments:
            content_type = (attachment.content_type or "").lower()
            if content_type.startswith("image/"):
                return attachment

            filename = attachment.filename.lower()
            if filename.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                return attachment

        return None

    @staticmethod
    def _normalize_name(name: str) -> str:
        return " ".join(name.strip().split())