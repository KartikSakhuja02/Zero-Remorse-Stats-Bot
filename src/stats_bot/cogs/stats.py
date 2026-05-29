from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

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
            connection.execute("INSERT OR IGNORE INTO players (name) VALUES (?)", (normalized,))

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
            cursor = connection.execute("INSERT INTO matches (note) VALUES (?)", (note,))
            match_id = cursor.lastrowid

            for entry in parsed_entries:
                player_row = connection.execute(
                    "SELECT id FROM players WHERE name = ?",
                    (entry.name,),
                ).fetchone()
                if player_row is None:
                    player_cursor = connection.execute(
                        "INSERT INTO players (name) VALUES (?)",
                        (entry.name,),
                    )
                    player_id = player_cursor.lastrowid
                else:
                    player_id = player_row["id"]

                connection.execute(
                    """
                    INSERT INTO player_match_stats (match_id, player_id, kills, is_mvp)
                    VALUES (?, ?, ?, ?)
                    """,
                    (match_id, player_id, entry.kills, int(entry.is_mvp)),
                )

        summary_lines = [
            f"Recorded match #{match_id} with {len(parsed_entries)} players.",
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
            row = connection.execute(
                """
                SELECT
                    p.name,
                    COUNT(pms.id) AS matches,
                    COALESCE(SUM(pms.kills), 0) AS kills,
                    COALESCE(SUM(pms.is_mvp), 0) AS mvps
                FROM players p
                LEFT JOIN player_match_stats pms ON pms.player_id = p.id
                WHERE p.name = ?
                GROUP BY p.id
                """,
                (normalized,),
            ).fetchone()

        if row is None:
            await interaction.response.send_message(f"No stats found for {normalized}.", ephemeral=True)
            return

        matches = int(row["matches"])
        kills = int(row["kills"])
        mvps = int(row["mvps"])
        km = kills / matches if matches else 0.0

        embed = discord.Embed(title=f"Stats for {row['name']}", color=discord.Color.blurple())
        embed.add_field(name="Matches", value=str(matches), inline=True)
        embed.add_field(name="MVP", value=str(mvps), inline=True)
        embed.add_field(name="Kills", value=str(kills), inline=True)
        embed.add_field(name="K/M", value=f"{km:.2f}", inline=True)

        await interaction.response.send_message(embed=embed)

    @stats.command(name="leaderboard", description="Show ranked scrim stats")
    @app_commands.describe(sort_by="Sort by kills, mvps, matches, or km")
    async def leaderboard(self, interaction: discord.Interaction, sort_by: str = "kills") -> None:
        sort_key = sort_by.strip().lower()
        sort_map = {
            "kills": "kills DESC",
            "mvps": "mvps DESC, kills DESC",
            "matches": "matches DESC, kills DESC",
            "km": "km DESC, kills DESC",
        }
        order_clause = sort_map.get(sort_key)
        if order_clause is None:
            await interaction.response.send_message(
                "sort_by must be one of: kills, mvps, matches, km",
                ephemeral=True,
            )
            return

        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    p.name,
                    COUNT(pms.id) AS matches,
                    COALESCE(SUM(pms.kills), 0) AS kills,
                    COALESCE(SUM(pms.is_mvp), 0) AS mvps,
                    CASE
                        WHEN COUNT(pms.id) = 0 THEN 0.0
                        ELSE CAST(SUM(pms.kills) AS REAL) / COUNT(pms.id)
                    END AS km
                FROM players p
                LEFT JOIN player_match_stats pms ON pms.player_id = p.id
                GROUP BY p.id
                ORDER BY {order_clause}, p.name COLLATE NOCASE
                LIMIT 10
                """
            ).fetchall()

        if not rows:
            await interaction.response.send_message("No players have been recorded yet.", ephemeral=True)
            return

        lines = [f"Top players by {sort_key}:"]
        for index, row in enumerate(rows, start=1):
            lines.append(
                f"{index}. {row['name']} - matches {int(row['matches'])}, mvps {int(row['mvps'])}, kills {int(row['kills'])}, km {float(row['km']):.2f}"
            )

        await interaction.response.send_message("\n".join(lines))

    @stats.command(name="recent", description="Show the latest recorded match")
    async def recent_match(self, interaction: discord.Interaction) -> None:
        with self.database.connect() as connection:
            match = connection.execute(
                "SELECT id, played_at, note FROM matches ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if match is None:
                await interaction.response.send_message("No matches recorded yet.", ephemeral=True)
                return

            rows = connection.execute(
                """
                SELECT p.name, pms.kills, pms.is_mvp
                FROM player_match_stats pms
                JOIN players p ON p.id = pms.player_id
                WHERE pms.match_id = ?
                ORDER BY pms.is_mvp DESC, pms.kills DESC, p.name COLLATE NOCASE
                """,
                (match["id"],),
            ).fetchall()

        lines = [f"Match #{match['id']} recorded at {match['played_at']}"]
        if match["note"]:
            lines.append(f"Note: {match['note']}")
        for row in rows:
            marker = " MVP" if row["is_mvp"] else ""
            lines.append(f"{row['name']}: {int(row['kills'])} kills{marker}")

        await interaction.response.send_message("\n".join(lines))

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
