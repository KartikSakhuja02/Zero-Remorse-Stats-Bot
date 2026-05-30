from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
import logging
from typing import Any

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
    local_path: str
    ocr_text: str
    player_name: str


class ProfileReviewView(discord.ui.View):
    def __init__(self, cog: "ProfileCog", submission_id: int, requester_id: int, player_name: str) -> None:
        super().__init__(timeout=3600)
        self.cog = cog
        self.submission_id = submission_id
        self.requester_id = requester_id
        self.player_name = player_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("This confirmation is not for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        await self.cog.handle_profile_decision(interaction, self.submission_id, self.player_name, approved=True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        await self.cog.handle_profile_decision(interaction, self.submission_id, self.player_name, approved=False)


class ProfileCog(commands.Cog):
    def __init__(self, bot: commands.Bot, database: Database, settings: Any) -> None:
        self.bot = bot
        self.database = database
        self.settings = settings
        self.ocr_client = None
        self._announcement_refreshed = False

        self.screenshot_dir = Path(settings.profile_screenshot_dir)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)

        if settings.openrouter_api_key:
            self.ocr_client = OpenRouterOCRClient(
                api_key=settings.openrouter_api_key,
                model=settings.openrouter_model,
                base_url=settings.openrouter_base_url,
                app_name=settings.openrouter_app_name,
                site_url=settings.openrouter_site_url,
            )

    @app_commands.command(name="profile_setup", description="Post the profile submission instructions in the submission channel")
    async def profile_setup(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        channel = await self._get_submission_channel()
        if channel is None:
            await interaction.followup.send("PROFILE_SUBMISSION_CHANNEL_ID is not configured.", ephemeral=True)
            return

        await self.refresh_profile_announcement(channel=channel)
        await interaction.followup.send(f"Posted instructions in {channel.mention}.", ephemeral=True)

    @app_commands.command(name="profile", description="Create or refresh a player's stats card in the profile stats channel")
    @app_commands.describe(member="The registered Discord member to show on the profile card")
    async def profile(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await interaction.response.defer(ephemeral=True)
        if self.settings.profile_stats_channel_id is None:
            await interaction.followup.send("PROFILE_STATS_CHANNEL_ID is not configured.", ephemeral=True)
            return

        result = await self.refresh_profile_card(member.id, create_if_missing=True)
        if result is None:
            await interaction.followup.send("I could not find a registered profile for that member.", ephemeral=True)
            return

        await interaction.followup.send(f"Updated profile card for {member.mention}.", ephemeral=True)

    async def refresh_profile_announcement(self, channel: discord.TextChannel | None = None) -> None:
        if self.settings.profile_submission_channel_id is None:
            return

        resolved_channel = channel or await self._get_submission_channel()
        if resolved_channel is None:
            return

        await self._purge_bot_messages(resolved_channel)

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

            local_path = await self._save_profile_screenshot_async(message.id, normalized_name, image_attachment)
            submission_id = self._save_submission(
                discord_user_id=message.author.id,
                source_channel_id=message.channel.id,
                source_message_id=message.id,
                attachment_url=image_attachment.url,
                local_path=local_path,
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

            view = ProfileReviewView(self, submission_id, message.author.id, normalized_name)
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
        submission = self._get_submission(submission_id)
        if submission is None:
            await self._safe_edit_interaction_message(interaction, "I could not find that submission.")
            return

        if approved:
            logger.info(
                "Approving profile submission %s for discord_user_id=%s player_name=%s",
                submission_id,
                submission["discord_user_id"],
                player_name,
            )
            self._upsert_player_profile(
                player_name=player_name,
                discord_user_id=submission["discord_user_id"],
                screenshot_path=submission["local_path"],
                ocr_text=submission["ocr_text"],
            )
            logger.info("Saved profile for %s", player_name)

        updated = self._mark_submission_reviewed(submission_id, approved)
        if not updated:
            await self._safe_edit_interaction_message(interaction, "This submission was already handled.")
            return

        message_text = (
            f"Approved. {player_name} is now registered."
            if approved
            else f"Declined. {player_name} was not registered."
        )
        await self._safe_edit_interaction_message(interaction, message_text)

        if approved:
            await self._finalize_approved_submission(submission, player_name)

    async def _finalize_approved_submission(self, submission: dict[str, Any], player_name: str) -> None:
        await self._delete_submission_message(submission["source_channel_id"], submission["source_message_id"])
        await self.refresh_profile_card(submission["discord_user_id"], create_if_missing=True)
        logger.info("Finalized approved profile for %s", player_name)

    async def refresh_profile_card(self, discord_user_id: int, create_if_missing: bool = False) -> bool | None:
        profile = self._get_profile_by_user(discord_user_id)
        if profile is None:
            return None

        channel = await self._get_profile_stats_channel()
        if channel is None:
            return None

        page_state = self._get_profile_page_state(profile["player_name"])
        if page_state is not None:
            with suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
                old_message = await channel.fetch_message(page_state["message_id"])
                await old_message.delete()
        elif not create_if_missing:
            return True

        content, file = self._build_profile_card(profile)
        kwargs: dict[str, Any] = {"content": content}
        if file is not None:
            kwargs["file"] = file

        new_message = await channel.send(**kwargs)
        self._save_profile_page_state(profile["player_name"], channel.id, new_message.id)
        return True

    async def refresh_profile_card_for_player(self, player_name: str) -> bool | None:
        profile = self._get_profile_by_name(player_name)
        if profile is None:
            return None

        channel = await self._get_profile_stats_channel()
        if channel is None:
            return None

        page_state = self._get_profile_page_state(player_name)
        if page_state is None:
            return True

        with suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
            old_message = await channel.fetch_message(page_state["message_id"])
            await old_message.delete()

        content, file = self._build_profile_card(profile)
        kwargs: dict[str, Any] = {"content": content}
        if file is not None:
            kwargs["file"] = file

        new_message = await channel.send(**kwargs)
        self._save_profile_page_state(player_name, channel.id, new_message.id)
        return True

    async def on_ready(self) -> None:
        if self._announcement_refreshed:
            return

        self._announcement_refreshed = True
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

    async def _get_profile_stats_channel(self) -> discord.TextChannel | None:
        if self.settings.profile_stats_channel_id is None:
            return None

        channel = self.bot.get_channel(self.settings.profile_stats_channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel

        fetched_channel = await self.bot.fetch_channel(self.settings.profile_stats_channel_id)
        if isinstance(fetched_channel, discord.TextChannel):
            return fetched_channel

        return None

    def _get_submission(self, submission_id: int) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, discord_user_id, source_channel_id, source_message_id, local_path, ocr_text, player_name, status
                    FROM profile_submissions
                    WHERE id = %s
                    """,
                    (submission_id,),
                )
                row = cursor.fetchone()

        return row

    def _mark_submission_reviewed(self, submission_id: int, approved: bool) -> bool:
        with self.database.connect() as connection:
            with connection.cursor() as cursor:
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

        return updated is not None

    def _upsert_player_profile(self, player_name: str, discord_user_id: int, screenshot_path: str, ocr_text: str) -> None:
        with self.database.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO player_stats (
                        player_name,
                        matches,
                        mvp,
                        kills,
                        discord_user_id,
                        registered_at,
                        screenshot_path,
                        ocr_text,
                        updated_at
                    )
                    VALUES (%s, 0, 0, 0, %s, NOW(), %s, %s, NOW())
                    ON CONFLICT (player_name) DO UPDATE
                    SET discord_user_id = EXCLUDED.discord_user_id,
                        screenshot_path = EXCLUDED.screenshot_path,
                        ocr_text = EXCLUDED.ocr_text,
                        registered_at = COALESCE(player_stats.registered_at, NOW()),
                        updated_at = NOW()
                    """,
                    (player_name, discord_user_id, screenshot_path, ocr_text),
                )

    def _save_submission(
        self,
        discord_user_id: int,
        source_channel_id: int,
        source_message_id: int,
        attachment_url: str,
        local_path: str,
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
                        local_path,
                        ocr_text,
                        player_name,
                        status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
                    RETURNING id
                    """,
                    (discord_user_id, source_channel_id, source_message_id, attachment_url, local_path, ocr_text, player_name),
                )
                row = cursor.fetchone()

        return int(row["id"])

    def _get_profile_by_user(self, discord_user_id: int) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT player_name, matches, mvp, kills, kill_per_match, discord_user_id, screenshot_path, ocr_text
                    FROM player_stats
                    WHERE discord_user_id = %s
                    """,
                    (discord_user_id,),
                )
                row = cursor.fetchone()

        return row

    def _get_profile_by_name(self, player_name: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT player_name, matches, mvp, kills, kill_per_match, discord_user_id, screenshot_path, ocr_text
                    FROM player_stats
                    WHERE player_name = %s
                    """,
                    (player_name,),
                )
                row = cursor.fetchone()

        return row

    def _get_profile_page_state(self, player_name: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT channel_id, message_id
                    FROM profile_pages
                    WHERE player_name = %s
                    """,
                    (player_name,),
                )
                row = cursor.fetchone()

        return row

    def _save_profile_page_state(self, player_name: str, channel_id: int, message_id: int) -> None:
        with self.database.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO profile_pages (player_name, channel_id, message_id, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (player_name) DO UPDATE
                    SET channel_id = EXCLUDED.channel_id,
                        message_id = EXCLUDED.message_id,
                        updated_at = NOW()
                    """,
                    (player_name, channel_id, message_id),
                )

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

    async def _delete_submission_message(self, channel_id: int, message_id: int) -> None:
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            fetched_channel = await self.bot.fetch_channel(channel_id)
            if not isinstance(fetched_channel, discord.TextChannel):
                return
            channel = fetched_channel

        try:
            message = await channel.fetch_message(message_id)
            await message.delete()
        except discord.NotFound:
            logger.warning("Submission message %s in channel %s was already deleted", message_id, channel_id)
        except discord.Forbidden:
            logger.warning("Missing permissions to delete submission message %s in channel %s", message_id, channel_id)
        except discord.HTTPException:
            logger.exception("Failed to delete submission message %s in channel %s", message_id, channel_id)

    async def _purge_bot_messages(self, channel: discord.TextChannel) -> None:
        bot_user = self.bot.user
        if bot_user is None:
            return

        with suppress(discord.Forbidden, discord.HTTPException):
            await channel.purge(limit=100, check=lambda message: message.author.id == bot_user.id)

    async def _safe_edit_interaction_message(self, interaction: discord.Interaction, content: str) -> None:
        message = interaction.message
        if message is None:
            return

        with suppress(discord.HTTPException):
            await message.edit(content=content, embed=None, view=None)

    async def _save_attachment_to_disk(self, attachment: discord.Attachment, file_path: Path) -> str:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(await attachment.read())
        return str(file_path)

    async def _save_profile_screenshot_async(self, message_id: int, player_name: str, attachment: discord.Attachment) -> str:
        suffix = self._file_suffix_for_attachment(attachment)
        safe_name = self._sanitize_filename(player_name)
        file_path = self.screenshot_dir / f"{message_id}_{safe_name}{suffix}"
        return await self._save_attachment_to_disk(attachment, file_path)

    def _build_profile_card(self, profile: dict[str, Any]) -> tuple[str, discord.File | None]:
        return self._build_profile_content(profile), self._build_profile_attachment(profile)

    def _build_profile_content(self, profile: dict[str, Any]) -> str:
        lines = [
            f"**Profile**: {profile['player_name']}",
            f"**Discord**: <@{profile['discord_user_id']}>" if profile["discord_user_id"] else "**Discord**: Not linked",
            f"**Matches**: {int(profile['matches'])}",
            f"**MVP**: {int(profile['mvp'])}",
            f"**Kills**: {int(profile['kills'])}",
            f"**K/M**: {float(profile['kill_per_match']):.2f}",
        ]
        if profile["ocr_text"]:
            lines.append(f"**OCR**: {self._truncate_text(profile['ocr_text'], 180)}")

        return "\n".join(lines)

    def _build_profile_attachment(self, profile: dict[str, Any]) -> discord.File | None:
        screenshot_path = profile["screenshot_path"]
        if not screenshot_path:
            return None

        screenshot_file = Path(screenshot_path)
        if not screenshot_file.exists():
            return None

        return discord.File(screenshot_file, filename=screenshot_file.name)

    @staticmethod
    def _truncate_text(text: str, limit: int = 1024) -> str:
        return text if len(text) <= limit else f"{text[: limit - 3]}..."

    @staticmethod
    def _file_suffix_for_attachment(attachment: discord.Attachment) -> str:
        content_type = (attachment.content_type or "").lower()
        if content_type.endswith("png"):
            return ".png"
        if content_type.endswith("jpeg") or content_type.endswith("jpg"):
            return ".jpg"
        if content_type.endswith("webp"):
            return ".webp"
        if content_type.endswith("gif"):
            return ".gif"
        filename = attachment.filename.lower()
        return next((suffix for suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif") if filename.endswith(suffix)), ".png")

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        allowed = [char if char.isalnum() or char in {"-", "_"} else "_" for char in name.strip()]
        return "".join(allowed) or "profile"

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
