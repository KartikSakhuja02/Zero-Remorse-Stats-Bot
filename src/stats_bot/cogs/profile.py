from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFilter, ImageFont

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

        if approved:
            await self._delete_submission_message(submission["source_channel_id"], submission["source_message_id"])

        message_text = (
            f"Approved. {player_name} is now registered."
            if approved
            else f"Declined. {player_name} was not registered."
        )
        await self._safe_edit_interaction_message(interaction, message_text)

        if approved:
            await self.refresh_profile_card(submission["discord_user_id"], create_if_missing=True)

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
        kwargs: dict[str, Any] = {"embed": embed}
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
        kwargs: dict[str, Any] = {"embed": embed}
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
                    SELECT id, discord_user_id, local_path, ocr_text, player_name, status
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

        with suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = await channel.fetch_message(message_id)
            await message.delete()

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
        player_name = profile["player_name"]
        matches = int(profile["matches"])
        mvps = int(profile["mvp"])
        kills = int(profile["kills"])
        kill_per_match = float(profile["kill_per_match"])
        discord_user_id = profile["discord_user_id"]
        screenshot_path = profile["screenshot_path"]
        ocr_text = profile["ocr_text"]
        card_bytes = self._render_profile_card_image(
            player_name=player_name,
            discord_user_id=discord_user_id,
            matches=matches,
            mvps=mvps,
            kills=kills,
            kill_per_match=kill_per_match,
            screenshot_path=screenshot_path,
            ocr_text=ocr_text,
        )

        embed = discord.Embed(title=f"Profile Card - {player_name}", color=discord.Color.teal())
        embed.set_image(url="attachment://profile-card.png")
        file = discord.File(card_bytes, filename="profile-card.png")
        return embed, file

    def _render_profile_card_image(
        self,
        *,
        player_name: str,
        discord_user_id: int | None,
        matches: int,
        mvps: int,
        kills: int,
        kill_per_match: float,
        screenshot_path: str | None,
        ocr_text: str,
    ) -> BytesIO:
        width, height = 1400, 820
        background = Image.new("RGBA", (width, height), (13, 17, 25, 255))
        draw = ImageDraw.Draw(background)

        self._draw_vertical_gradient(draw, width, height, (13, 17, 25), (23, 31, 45))
        self._draw_glow(draw, width, height)

        panel_color = (20, 27, 39, 240)
        accent = (99, 230, 190, 255)
        accent_soft = (72, 164, 141, 255)

        self._rounded_panel(draw, (40, 40, 1360, 780), radius=36, fill=panel_color, outline=(55, 70, 90, 255))
        self._rounded_panel(draw, (70, 90, 430, 750), radius=28, fill=(16, 22, 32, 255), outline=(70, 86, 106, 255))
        self._rounded_panel(draw, (470, 90, 1320, 750), radius=28, fill=(18, 24, 35, 255), outline=(70, 86, 106, 255))

        title_font = self._load_font(54, bold=True)
        subtitle_font = self._load_font(22)
        stat_label_font = self._load_font(20, bold=True)
        stat_value_font = self._load_font(34, bold=True)
        small_font = self._load_font(18)
        mono_font = self._load_font(18)

        draw.text((110, 112), player_name, font=title_font, fill=(245, 249, 255, 255))
        draw.text((110, 175), f"Discord User: <@{discord_user_id}>" if discord_user_id else "Discord User: Not linked", font=subtitle_font, fill=(195, 206, 220, 255))
        draw.rounded_rectangle((110, 220, 285, 262), radius=16, fill=accent)
        draw.text((145, 228), "REGISTERED", font=self._load_font(18, bold=True), fill=(10, 18, 21, 255))

        self._draw_metric_card(draw, 500, 120, 250, 132, "MATCHES", str(matches), accent_soft, stat_label_font, stat_value_font)
        self._draw_metric_card(draw, 770, 120, 250, 132, "MVP", str(mvps), (214, 156, 72, 255), stat_label_font, stat_value_font)
        self._draw_metric_card(draw, 1040, 120, 250, 132, "KILLS", str(kills), (108, 168, 255, 255), stat_label_font, stat_value_font)

        self._draw_metric_card(draw, 500, 280, 790, 124, "K/M", f"{kill_per_match:.2f}", (163, 117, 255, 255), stat_label_font, stat_value_font)

        self._draw_table_header(draw, 500, 462, 790, 34, (159, 173, 191, 255), small_font)
        self._draw_table_row(draw, 500, 502, 790, 62, player_name, matches, mvps, kills, kill_per_match, (245, 249, 255, 255), mono_font)

        draw.text((500, 592), "OCR TEXT", font=self._load_font(24, bold=True), fill=(159, 173, 191, 255))
        ocr_box = (500, 630, 1260, 708)
        self._rounded_panel(draw, ocr_box, radius=20, fill=(12, 17, 24, 255), outline=(60, 74, 92, 255))
        self._draw_multiline_text_box(draw, (520, 648), self._truncate_text(ocr_text, 180), mono_font, (233, 239, 245, 255), 720)

        screenshot_image = self._load_screenshot_image(screenshot_path)
        if screenshot_image is not None:
            screenshot_image = screenshot_image.convert("RGBA")
            screenshot_image.thumbnail((290, 360), Image.Resampling.LANCZOS)
            frame_w, frame_h = 320, 420
            frame_x, frame_y = 90, 300
            self._rounded_panel(draw, (frame_x, frame_y, frame_x + frame_w, frame_y + frame_h), radius=24, fill=(10, 14, 20, 255), outline=(82, 97, 118, 255))
            image_x = frame_x + (frame_w - screenshot_image.width) // 2
            image_y = frame_y + (frame_h - screenshot_image.height) // 2
            background.paste(screenshot_image, (image_x, image_y), screenshot_image)
        else:
            self._rounded_panel(draw, (90, 300, 410, 720), radius=24, fill=(10, 14, 20, 255), outline=(82, 97, 118, 255))
            draw.text((145, 490), "NO SCREENSHOT", font=subtitle_font, fill=(160, 174, 191, 255))

        footer = f"Profile generated for {player_name} | updated live when scrim results are submitted"
        draw.text((500, 732), footer, font=small_font, fill=(146, 158, 174, 255))

        output = BytesIO()
        background = background.convert("RGB")
        background.save(output, format="PNG", optimize=True)
        output.seek(0)
        return output

    @staticmethod
    def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        font_candidates = [
            "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
            "arialbd.ttf" if bold else "arial.ttf",
        ]
        for candidate in font_candidates:
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
        return ImageFont.load_default()

    @staticmethod
    def _draw_vertical_gradient(draw: ImageDraw.ImageDraw, width: int, height: int, top_rgb: tuple[int, int, int], bottom_rgb: tuple[int, int, int]) -> None:
        for y in range(height):
            ratio = y / max(height - 1, 1)
            red = int(top_rgb[0] * (1 - ratio) + bottom_rgb[0] * ratio)
            green = int(top_rgb[1] * (1 - ratio) + bottom_rgb[1] * ratio)
            blue = int(top_rgb[2] * (1 - ratio) + bottom_rgb[2] * ratio)
            draw.line((0, y, width, y), fill=(red, green, blue, 255))

    @staticmethod
    def _draw_glow(draw: ImageDraw.ImageDraw, width: int, height: int) -> None:
        draw.ellipse((930, -120, 1500, 480), fill=(48, 179, 153, 45))
        draw.ellipse((-220, 430, 300, 980), fill=(107, 107, 255, 24))

    @staticmethod
    def _rounded_panel(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], *, radius: int, fill: tuple[int, int, int, int], outline: tuple[int, int, int, int]) -> None:
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=2)

    def _draw_metric_card(
        self,
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        width: int,
        height: int,
        label: str,
        value: str,
        accent: tuple[int, int, int, int],
        label_font: ImageFont.ImageFont,
        value_font: ImageFont.ImageFont,
    ) -> None:
        self._rounded_panel(draw, (x, y, x + width, y + height), radius=22, fill=(12, 17, 24, 255), outline=accent)
        draw.text((x + 24, y + 18), label, font=label_font, fill=(170, 182, 198, 255))
        draw.text((x + 24, y + 54), value, font=value_font, fill=(245, 249, 255, 255))

    @staticmethod
    def _draw_table_header(draw: ImageDraw.ImageDraw, x: int, y: int, width: int, height: int, fill: tuple[int, int, int, int], font: ImageFont.ImageFont) -> None:
        columns = ["NAME", "MATCHES", "MVP", "KILLS", "K/M"]
        positions = [0.00, 0.38, 0.58, 0.74, 0.89]
        for column, position in zip(columns, positions, strict=False):
            draw.text((x + int(width * position), y), column, font=font, fill=fill)
        draw.line((x, y + height - 2, x + width, y + height - 2), fill=(82, 97, 118, 255), width=2)

    @staticmethod
    def _draw_table_row(
        draw: ImageDraw.ImageDraw,
        x: int,
        y: int,
        width: int,
        height: int,
        player_name: str,
        matches: int,
        mvps: int,
        kills: int,
        kill_per_match: float,
        fill: tuple[int, int, int, int],
        font: ImageFont.ImageFont,
    ) -> None:
        values = [player_name, str(matches), str(mvps), str(kills), f"{kill_per_match:.2f}"]
        positions = [0.00, 0.38, 0.58, 0.74, 0.89]
        for value, position in zip(values, positions, strict=False):
            draw.text((x + int(width * position), y + 12), value, font=font, fill=fill)

    @staticmethod
    def _draw_multiline_text_box(draw: ImageDraw.ImageDraw, position: tuple[int, int], text: str, font: ImageFont.ImageFont, fill: tuple[int, int, int, int], max_width: int) -> None:
        x, y = position
        words = text.split()
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
            current = word
        if current:
            lines.append(current)

        for index, line in enumerate(lines[:4]):
            draw.text((x, y + index * 24), line, font=font, fill=fill)

    @staticmethod
    def _load_screenshot_image(screenshot_path: str | None) -> Image.Image | None:
        if not screenshot_path:
            return None

        file_path = Path(screenshot_path)
        if not file_path.exists():
            return None

        return Image.open(file_path)

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
