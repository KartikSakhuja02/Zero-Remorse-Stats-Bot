from __future__ import annotations

import logging
from typing import Sequence

import discord
from discord import app_commands
from discord.ext import commands

from .config import load_settings
from .db import Database
from .cogs.stats import StatsCog

logger = logging.getLogger(__name__)


class ScrimBot(commands.Bot):
    def __init__(self, database: Database, guild_id: int | None) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.database = database
        self.guild_id = guild_id

    async def setup_hook(self) -> None:
        await self.add_cog(StatsCog(self, self.database))

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


async def run_bot() -> None:
    settings = load_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))

    database = Database(settings.database_path)
    bot = ScrimBot(database=database, guild_id=settings.guild_id)
    async with bot:
        await bot.start(settings.token)


def main() -> None:
    import asyncio

    asyncio.run(run_bot())
