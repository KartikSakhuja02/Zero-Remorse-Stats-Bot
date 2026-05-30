from __future__ import annotations

import logging

import discord
from discord.ext import commands

from .config import load_settings
from .db import Database
from .cogs.stats import StatsCog
from .cogs.profile import ProfileCog

logger = logging.getLogger(__name__)


class ScrimBot(commands.Bot):
    def __init__(self, database: Database, settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.database = database
        self.settings = settings
        self.guild_id = settings.guild_id
        self._startup_refresh_done = False

    async def setup_hook(self) -> None:
        await self.add_cog(StatsCog(self, self.database))
        await self.add_cog(ProfileCog(self, self.database, self.settings))

        profile_cog = self.get_cog("ProfileCog")
        if profile_cog is not None:
            self.add_view(profile_cog.create_card_view())

        if self.guild_id is not None:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %s commands to guild %s", len(synced), self.guild_id)
        else:
            synced = await self.tree.sync()
            logger.info("Synced %s global commands", len(synced))

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")

        if self._startup_refresh_done:
            return

        self._startup_refresh_done = True
        profile_cog = self.get_cog("ProfileCog")
        if profile_cog is not None:
            await profile_cog.refresh_profile_announcement()


async def run_bot() -> None:
    settings = load_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))

    database = Database(settings.database_url)
    bot = ScrimBot(database=database, settings=settings)
    async with bot:
        await bot.start(settings.token)


def main() -> None:
    import asyncio

    asyncio.run(run_bot())
