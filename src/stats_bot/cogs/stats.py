from __future__ import annotations

from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

from ..db import Database


@dataclass(frozen=True)
class MatchEntry:
    name: str
    kills: int
    is_mvp: bool


class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot, database: Database) -> None:
        self.bot = bot
        self.database = database

    stats = app_commands.Group(name="stats", description="Player and scrim statistics")

    @app_commands.command(name="add_player", description="Register a player name if it does not exist")
    @app_commands.describe(name="Player name to store")
    async def add_player(self, interaction: discord.Interaction, name: str) -> None:
        normalized = self._normalize_name(name)
        with self.database.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO player_stats (player_name)
                    VALUES (%s)
                    ON CONFLICT (player_name) DO NOTHING
                    """,
                    (normalized,),
                )

        await interaction.response.send_message(f"Saved player: {normalized}", ephemeral=True)

    @app_commands.command(
        name="record_match",
        description="Record one scrim match from a compact entry list",
    )
    @app_commands.describe(
        entries="Format: player:kills[:mvp] | player:kills[:mvp]",
        note="Optional match note, such as map or opponent",
    )
    async def record_match(self, interaction: discord.Interaction, entries: str, note: str | None = None) -> None:
        parsed_entries = self._parse_entries(entries)
        if not parsed_entries:
            await interaction.response.send_message(
                "No valid player entries found. Use: name:kills[:mvp] | name:kills[:mvp]",
                ephemeral=True,
            )
            return

        with self.database.connect() as connection:
            with connection.cursor() as cursor:
                for entry in parsed_entries:
                    cursor.execute(
                        """
                        INSERT INTO player_stats (player_name, matches, mvp, kills)
                        VALUES (%s, 1, %s, %s)
                        ON CONFLICT (player_name) DO UPDATE
                        SET
                            matches = player_stats.matches + 1,
                            mvp = player_stats.mvp + EXCLUDED.mvp,
                            kills = player_stats.kills + EXCLUDED.kills,
                            updated_at = NOW()
                        """,
                        (entry.name, int(entry.is_mvp), entry.kills),
                    )

        summary_lines = [
            f"Recorded match totals for {len(parsed_entries)} players.",
            *[
                f"{entry.name}: {entry.kills} kills{' and MVP' if entry.is_mvp else ''}"
                for entry in parsed_entries
            ],
        ]
        if note:
            summary_lines.append(f"Note: {note}")

        await interaction.response.send_message("\n".join(summary_lines))

    @stats.command(name="player", description="Show a player's total scrim stats")
    @app_commands.describe(name="Player name to look up")
    async def player_stats(self, interaction: discord.Interaction, name: str) -> None:
        normalized = self._normalize_name(name)
        with self.database.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        player_name,
                        matches,
                        mvp,
                        kills,
                        kill_per_match
                    FROM player_stats
                    WHERE player_name = %s
                    """,
                    (normalized,),
                )
                row = cursor.fetchone()

        if row is None:
            await interaction.response.send_message(f"No stats found for {normalized}.", ephemeral=True)
            return

        embed = discord.Embed(title=f"Stats for {row['player_name']}", color=discord.Color.blurple())
        embed.add_field(name="Matches", value=str(int(row["matches"])), inline=True)
        embed.add_field(name="MVP", value=str(int(row["mvp"])), inline=True)
        embed.add_field(name="Kills", value=str(int(row["kills"])), inline=True)
        embed.add_field(name="K/M", value=f"{float(row['kill_per_match']):.2f}", inline=True)

        await interaction.response.send_message(embed=embed)

    @stats.command(name="leaderboard", description="Show ranked scrim stats")
    @app_commands.describe(sort_by="Sort by kills, mvps, matches, or km")
    async def leaderboard(self, interaction: discord.Interaction, sort_by: str = "kills") -> None:
        sort_key = sort_by.strip().lower()
        sort_map = {
            "kills": "kills DESC",
            "mvps": "mvp DESC, kills DESC",
            "matches": "matches DESC, kills DESC",
            "km": "kill_per_match DESC, kills DESC",
        }
        order_clause = sort_map.get(sort_key)
        if order_clause is None:
            await interaction.response.send_message(
                "sort_by must be one of: kills, mvps, matches, km",
                ephemeral=True,
            )
            return

        with self.database.connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT
                        player_name,
                        matches,
                        mvp,
                        kills,
                        kill_per_match
                    FROM player_stats
                    ORDER BY {order_clause}, lower(player_name)
                    LIMIT 10
                    """
                )
                rows = cursor.fetchall()

        if not rows:
            await interaction.response.send_message("No players have been recorded yet.", ephemeral=True)
            return

        lines = [f"Top players by {sort_key}:"]
        for index, row in enumerate(rows, start=1):
            lines.append(
                f"{index}. {row['player_name']} - matches {int(row['matches'])}, mvps {int(row['mvp'])}, kills {int(row['kills'])}, km {float(row['kill_per_match']):.2f}"
            )

        await interaction.response.send_message("\n".join(lines))

    @stats.command(name="recent", description="Show the latest recorded match")
    async def recent_match(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "This version stores aggregate player totals only, so individual match history is not tracked.",
            ephemeral=True,
        )

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.stats)
        self.bot.tree.add_command(self.add_player)
        self.bot.tree.add_command(self.record_match)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.stats.name, type=self.stats.type)
        self.bot.tree.remove_command(self.add_player.name, type=self.add_player.type)
        self.bot.tree.remove_command(self.record_match.name, type=self.record_match.type)

    @staticmethod
    def _normalize_name(name: str) -> str:
        return " ".join(name.strip().split())

    @staticmethod
    def _parse_entries(entries: str) -> list[MatchEntry]:
        parsed_entries: list[MatchEntry] = []
        for raw_entry in entries.split("|"):
            entry = raw_entry.strip()
            if not entry:
                continue

            parts = [part.strip() for part in entry.split(":")]
            if len(parts) < 2:
                continue

            name = " ".join(parts[0].split())
            if not name:
                continue

            try:
                kills = int(parts[1])
            except ValueError:
                continue

            is_mvp = False
            if len(parts) >= 3:
                is_mvp = parts[2].lower() in {"1", "true", "yes", "y", "mvp"}

            parsed_entries.append(MatchEntry(name=name, kills=kills, is_mvp=is_mvp))

        return parsed_entries
