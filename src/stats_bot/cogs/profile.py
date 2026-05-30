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
from ..tracker import TrackerClient, TrackerProfile

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


class ProfileCardView(discord.ui.View):
    def __init__(self, cog: "ProfileCog", tracker_url: str | None = None) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        if tracker_url:
            self.add_item(discord.ui.Button(label="Tracker", style=discord.ButtonStyle.link, url=tracker_url))

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, custom_id="profile_card_refresh")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        profile_name = self._extract_profile_name(interaction)
        if profile_name is None:
            await interaction.response.send_message("I could not tell which profile this is.", ephemeral=True)
            return

        refreshed = await self.cog.refresh_profile_card_for_player(profile_name)
        if refreshed is None:
            await interaction.response.send_message("That profile is not registered yet.", ephemeral=True)
            return

        await interaction.response.send_message(f"Refreshed profile for {profile_name}.", ephemeral=True)

    @discord.ui.button(label="Screenshot", style=discord.ButtonStyle.primary, custom_id="profile_card_screenshot")
    async def screenshot(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        message = interaction.message
        if message is None or not message.attachments:
            await interaction.response.send_message("No screenshot is attached to this profile post.", ephemeral=True)
            return

        attachment = message.attachments[0]
        embed = discord.Embed(title="Profile Screenshot", color=discord.Color.blurple())
        embed.set_image(url=attachment.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @staticmethod
    def _extract_profile_name(interaction: discord.Interaction) -> str | None:
        message = interaction.message
        if message is None or not message.embeds:
            return None
        embed = message.embeds[0]
        if not embed.title:
            return None

        prefix = "Profile Card - "
        if not embed.title.startswith(prefix):
            return None

        return embed.title[len(prefix) :].strip() or None


class TrackerLinkConfirmView(discord.ui.View):
    def __init__(self, cog: "ProfileCog", requester_id: int, tracker_profile: TrackerProfile) -> None:
        super().__init__(timeout=600)
        self.cog = cog
        self.requester_id = requester_id
        self.tracker_profile = tracker_profile

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("This confirmation is not for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, that's me", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        await self.cog.confirm_tracker_link(interaction, self.tracker_profile)

    @discord.ui.button(label="No, not me", style=discord.ButtonStyle.red)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        await interaction.edit_original_response(
            content="No link was saved. If this was the wrong profile, try `/trn` again with the correct IGN.",
            embed=None,
            view=None,
        )


class ProfileCog(commands.Cog):
    def __init__(self, bot: commands.Bot, database: Database, settings: Any) -> None:
        self.bot = bot
        self.database = database
        self.settings = settings
        self.ocr_client = None
        self.tracker_client = None
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

        if settings.tracker_api_key and settings.tracker_title_slug and settings.tracker_platform:
            self.tracker_client = TrackerClient(
                api_key=settings.tracker_api_key,
                title_slug=settings.tracker_title_slug,
                platform=settings.tracker_platform,
                base_url=settings.tracker_base_url,
            )

    def create_card_view(self) -> ProfileCardView:
        return ProfileCardView(self)

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

    @app_commands.command(name="submit", description="Submit 1 to 3 match screenshots to update your registered stats")
    @app_commands.describe(
        ss1="First screenshot",
        ss2="Second screenshot",
        ss3="Third screenshot",
    )
    async def submit(
        self,
        interaction: discord.Interaction,
        ss1: discord.Attachment,
        ss2: discord.Attachment | None = None,
        ss3: discord.Attachment | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if self.ocr_client is None:
            await interaction.followup.send("OCR is not configured yet.", ephemeral=True)
            return

        profile = self._get_profile_by_user(interaction.user.id)
        if profile is None:
            await interaction.followup.send("You are not linked yet. Use `/trn` or the profile setup flow first.", ephemeral=True)
            return

        attachments = [attachment for attachment in (ss1, ss2, ss3) if attachment is not None]
        if not attachments:
            await interaction.followup.send("Attach at least one screenshot.", ephemeral=True)
            return

        kills_by_screenshot: list[tuple[str, int]] = []
        total_kills = 0
        for attachment in attachments:
            image_bytes = await attachment.read()
            kills = await self.ocr_client.extract_kills_for_player(
                image_bytes,
                profile["player_name"],
                attachment.content_type or "image/png",
            )
            kills_by_screenshot.append((attachment.filename, kills))
            total_kills += kills

        self._apply_match_submission(profile["player_name"], interaction.user.id, len(attachments), total_kills)

        refreshed = await self.refresh_profile_card(interaction.user.id, create_if_missing=True)
        lines = [
            f"Updated {profile['player_name']} from {len(attachments)} screenshot(s).",
            *[f"{filename}: {kills} kills" for filename, kills in kills_by_screenshot],
            f"Total kills added: {total_kills}",
        ]
        if refreshed is None:
            lines.append("I updated the database, but I could not refresh the profile card because the stats channel is not configured.")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="trn", description="Link your Discord account to your Tracker IGN")
    @app_commands.describe(myign="Your Tracker IGN")
    async def trn(self, interaction: discord.Interaction, myign: str) -> None:
        await interaction.response.defer(ephemeral=True)

        normalized_ign = self._normalize_name(myign)
        if not normalized_ign:
            await interaction.followup.send("Please provide a valid IGN.", ephemeral=True)
            return

        if self.tracker_client is None:
            await interaction.followup.send(
                "Tracker lookup is not configured yet. Set `TRACKER_API_KEY`, `TRACKER_TITLE_SLUG`, and `TRACKER_PLATFORM` first.",
                ephemeral=True,
            )
            return

        try:
            tracker_profile = await self.tracker_client.fetch_profile(normalized_ign)
        except Exception as exc:
            logger.exception("Tracker lookup failed for %s", normalized_ign)
            error_message = str(exc)
            if "401" in error_message or "Invalid authentication credentials" in error_message:
                await interaction.followup.send(
                    "Tracker auth failed. Check that `TRACKER_API_KEY` is the app key from your Tracker developer app, not a bearer token, and restart the bot.",
                    ephemeral=True,
                )
                return

            await interaction.followup.send(f"I could not find that Tracker profile: {exc}", ephemeral=True)
            return

        embed = self._build_tracker_preview_embed(tracker_profile)
        view = TrackerLinkConfirmView(self, interaction.user.id, tracker_profile)
        await interaction.followup.send(
            content="Is this your profile? If yes, I will register it to your Discord account.",
            embed=embed,
            view=view,
            ephemeral=True,
        )

    async def confirm_tracker_link(self, interaction: discord.Interaction, tracker_profile: TrackerProfile) -> None:
        self._link_discord_user_to_profile(tracker_profile.display_name, interaction.user.id)

        refreshed = await self.refresh_profile_card(interaction.user.id, create_if_missing=True)
        if refreshed is None:
            await interaction.edit_original_response(
                content=f"Linked to {tracker_profile.display_name}, but I could not refresh the profile card because the stats channel is not configured.",
                embed=None,
                view=None,
            )
            return

        await interaction.edit_original_response(
            content=f"Linked your Discord account to {tracker_profile.display_name}.",
            embed=None,
            view=None,
        )

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

        embed, file = self._build_profile_card(profile)
        kwargs: dict[str, Any] = {"embed": embed, "view": self.create_card_view()}
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

        embed, file = self._build_profile_card(profile)
        kwargs: dict[str, Any] = {"embed": embed, "view": self.create_card_view()}
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
                    ORDER BY updated_at DESC, player_name ASC
                    LIMIT 1
                    """,
                    (discord_user_id,),
                )
                row = cursor.fetchone()

        return row

    def _link_discord_user_to_profile(self, player_name: str, discord_user_id: int) -> None:
        with self.database.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE player_stats
                    SET discord_user_id = NULL,
                        updated_at = NOW()
                    WHERE discord_user_id = %s
                      AND player_name <> %s
                    """,
                    (discord_user_id, player_name),
                )
                cursor.execute(
                    """
                    INSERT INTO player_stats (
                        player_name,
                        matches,
                        mvp,
                        kills,
                        discord_user_id,
                        registered_at,
                        updated_at
                    )
                    VALUES (%s, 0, 0, 0, %s, NOW(), NOW())
                    ON CONFLICT (player_name) DO UPDATE
                    SET discord_user_id = EXCLUDED.discord_user_id,
                        registered_at = COALESCE(player_stats.registered_at, NOW()),
                        updated_at = NOW()
                    """,
                    (player_name, discord_user_id),
                )

    def _apply_match_submission(self, player_name: str, discord_user_id: int, matches_added: int, kills_added: int) -> None:
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
                        updated_at
                    )
                    VALUES (%s, %s, 0, %s, %s, COALESCE((SELECT registered_at FROM player_stats WHERE player_name = %s), NOW()), NOW())
                    ON CONFLICT (player_name) DO UPDATE
                    SET matches = player_stats.matches + EXCLUDED.matches,
                        kills = player_stats.kills + EXCLUDED.kills,
                        discord_user_id = EXCLUDED.discord_user_id,
                        updated_at = NOW()
                    """,
                    (player_name, matches_added, kills_added, discord_user_id, player_name),
                )

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

        guild = channel.guild
        bot_member = guild.get_member(self.bot.user.id) if guild is not None and self.bot.user is not None else None
        if bot_member is not None and not channel.permissions_for(bot_member).manage_messages:
            logger.warning("Bot is missing Manage Messages in #%s, so it cannot delete submission message %s", channel.name, message_id)
            return

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

    def _build_profile_card(self, profile: dict[str, Any]) -> tuple[discord.Embed, discord.File | None]:
        embed = discord.Embed(
            title=f"Profile Card - {profile['player_name']}",
            color=discord.Color.teal(),
            description="Live profile snapshot with the submitted screenshot attached.",
        )
        embed.add_field(name="Discord", value=f"<@{profile['discord_user_id']}>" if profile["discord_user_id"] else "Not linked", inline=True)
        embed.add_field(name="Matches", value=str(int(profile["matches"])), inline=True)
        embed.add_field(name="MVP", value=str(int(profile["mvp"])), inline=True)
        embed.add_field(name="Kills", value=str(int(profile["kills"])), inline=True)
        embed.add_field(name="K/M", value=f"{float(profile['kill_per_match']):.2f}", inline=True)

        if profile["ocr_text"]:
            embed.add_field(name="OCR", value=self._truncate_text(profile["ocr_text"], 900), inline=False)

        embed.set_footer(text="Updated when scrim results are submitted")

        screenshot_file = self._build_profile_attachment(profile)
        if screenshot_file is not None:
            embed.set_image(url=f"attachment://{screenshot_file.filename}")

        return embed, screenshot_file

    def _build_profile_attachment(self, profile: dict[str, Any]) -> discord.File | None:
        screenshot_path = profile["screenshot_path"]
        if not screenshot_path:
            return None

        screenshot_file = Path(screenshot_path)
        if not screenshot_file.exists():
            return None

        return discord.File(screenshot_file, filename=screenshot_file.name)

    @staticmethod
    def _build_tracker_preview_embed(tracker_profile: TrackerProfile) -> discord.Embed:
        embed = discord.Embed(
            title=f"Tracker Profile Preview - {tracker_profile.display_name}",
            url=tracker_profile.profile_url,
            color=discord.Color.orange(),
            description="Check the stats below and confirm whether this is your profile.",
        )
        embed.add_field(name="IGN", value=tracker_profile.display_name, inline=True)
        embed.add_field(name="Platform", value=tracker_profile.platform, inline=True)
        embed.add_field(name="Tracker", value=tracker_profile.title_slug, inline=True)

        if stats_lines := ProfileCog._format_tracker_stats(tracker_profile.stats):
            embed.add_field(name="Stats", value="\n".join(stats_lines), inline=False)

        embed.set_footer(text="Yes, that's me = save it. No, not me = nothing is linked.")
        return embed

    @staticmethod
    def _format_tracker_stats(stats: dict[str, str]) -> list[str]:
        preferred_keys = ["matchesPlayed", "wins", "kills", "deaths", "kdRatio", "score"]
        friendly_names = {
            "matchesPlayed": "Matches",
            "wins": "Wins",
            "kills": "Kills",
            "deaths": "Deaths",
            "kdRatio": "K/D",
            "score": "Score",
        }

        lines: list[str] = []
        for key in preferred_keys:
            value = stats.get(key)
            if value is None:
                continue
            lines.append(f"**{friendly_names.get(key, key)}**: {value}")

        if lines:
            return lines

        return lines or [f"**{key}**: {value}" for key, value in list(stats.items())[:4]]

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
