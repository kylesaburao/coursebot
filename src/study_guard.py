"""In-memory Study Time eligibility and role reconciliation."""
import asyncio
from contextlib import suppress
from dataclasses import dataclass
import logging
import math
from time import monotonic

import discord
from discord.ext import commands, tasks

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StudyGuardConfig:
    guild_id: int
    voice_channel_id: int
    moderation_channel_id: int
    role_name: str
    join_limit_count: int = 5
    join_window_seconds: int = 60
    short_stay_seconds: int = 30
    short_stay_threshold: int = 5
    short_stay_window_seconds: int = 120


def prune_timestamps(timestamps, window_start):
    return [stamp for stamp in timestamps if stamp > window_start]


def wait_seconds(history, count, window, now):
    current = prune_timestamps(history, now - window)
    return max(0, current[-count] + window - now) if len(current) >= count else 0


class StudyGuard(commands.Cog):
    def __init__(self, bot, config):
        self.bot = bot
        self.config = config
        self.join_history = {}
        self.short_stay_history = {}
        # None denotes a startup occupant with unknown arrival time.
        self.active_visits = {}
        self.denied_visits = set()
        self.lock = asyncio.Lock()
        self.role = self.channel = self.report_channel = None

    def prune(self, now):
        for history, window in ((self.join_history, self.config.join_window_seconds), (self.short_stay_history, self.config.short_stay_window_seconds)):
            for user_id, stamps in list(history.items()):
                remaining = prune_timestamps(stamps, now - window)
                if remaining:
                    history[user_id] = remaining
                else:
                    del history[user_id]

    def join(self, user_id, now):
        self.prune(now)
        wait = math.ceil(max(
            wait_seconds(self.join_history.get(user_id, []), self.config.join_limit_count, self.config.join_window_seconds, now),
            wait_seconds(self.short_stay_history.get(user_id, []), self.config.short_stay_threshold, self.config.short_stay_window_seconds, now),
        ))
        if user_id in self.denied_visits:
            return wait
        if wait:
            self.denied_visits.add(user_id)
        else:
            self.join_history.setdefault(user_id, []).append(now)
            self.active_visits[user_id] = now
        return wait

    def leave(self, user_id, now):
        arrival = self.active_visits.pop(user_id, None)
        self.denied_visits.discard(user_id)
        self.prune(now)
        if arrival is not None and now - arrival < self.config.short_stay_seconds:
            self.short_stay_history.setdefault(user_id, []).append(now)

    def resolve(self):
        guild = self.bot.get_guild(self.config.guild_id)
        self.role = self.channel = self.report_channel = None
        if guild is None or guild.unavailable:
            log.warning('Study Guard guild %s unavailable', self.config.guild_id)
            return None
        channel = guild.get_channel(self.config.voice_channel_id)
        role = discord.utils.get(guild.roles, name=self.config.role_name)
        report = guild.get_channel(self.config.moderation_channel_id)
        if isinstance(report, discord.TextChannel) and guild.me and report.permissions_for(guild.me).send_messages and report.permissions_for(guild.me).view_channel:
            self.report_channel = report
        else:
            log.warning('Study Guard report channel %s unavailable or not writable', self.config.moderation_channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            log.warning('Study Guard voice channel %s unavailable', self.config.voice_channel_id)
            return guild
        self.channel = channel
        if not role or not guild.me or not guild.me.guild_permissions.manage_roles or not role.is_assignable():
            log.warning('Study Guard role %s unavailable or not assignable', self.config.role_name)
            return guild
        self.role = role
        return guild

    async def change_role(self, member, eligible):
        if self.role is None:
            return False
        try:
            if eligible:
                await member.add_roles(self.role, reason='Study Time eligibility')
            else:
                await member.remove_roles(self.role, reason='Study Time eligibility')
            return True
        except discord.HTTPException:
            log.warning('Study Guard role change failed for %s', member.id, exc_info=True)
            return False

    async def notify(self, member, wait):
        message = f'Please wait {wait}s, then leave and rejoin Study Time to receive the chat role.'
        try:
            await member.send(message)
        except discord.HTTPException:
            log.warning('Study Guard DM failed for %s', member.id)
        if self.report_channel:
            try:
                await self.report_channel.send(f'[STUDY TIME] {member.id} denied: {message}')
            except discord.HTTPException:
                log.warning('Study Guard report failed for %s', member.id)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        now = monotonic()
        if member.guild.id != self.config.guild_id:
            return
        old = before.channel.id if before.channel else None
        new = after.channel.id if after.channel else None
        channel_id = self.config.voice_channel_id
        if old == new or channel_id not in (old, new):
            return
        await self.bot.wait_until_ready()
        wait = 0
        async with self.lock:
            self.resolve()
            if new == channel_id:
                wait = self.join(member.id, now)
                await self.change_role(member, member.id not in self.denied_visits)
            else:
                self.leave(member.id, now)
                # Always issue transitions, even when the gateway role cache lags.
                await self.change_role(member, False)
        if wait:
            await self.notify(member, wait)

    async def reconcile(self):
        async with self.lock:
            now = monotonic()
            self.prune(now)
            guild = self.resolve()
            if guild is None or self.channel is None:
                return
            members = {member.id: member for member in guild.members}
            occupants = {member.id for member in self.channel.members}
            for history in (self.join_history, self.short_stay_history, self.active_visits):
                for user_id in list(history):
                    if user_id not in members:
                        del history[user_id]
            self.denied_visits.intersection_update(members)
            for user_id in set(self.active_visits) | self.denied_visits:
                if user_id not in occupants:
                    # No departure timestamp was observed while disconnected.
                    self.active_visits.pop(user_id, None)
                    self.denied_visits.discard(user_id)
            for user_id in occupants - self.denied_visits:
                self.active_visits.setdefault(user_id, None)
            added = removed = 0
            if self.role:
                for member in members.values():
                    eligible = member.id in occupants and member.id not in self.denied_visits
                    if bool(member.get_role(self.role.id)) != eligible:
                        if await self.change_role(member, eligible):
                            added += int(eligible)
                            removed += int(not eligible)
            log.info('Study Guard reconciliation: added=%s removed=%s', added, removed)

    @tasks.loop(seconds=600)
    async def maintenance(self):
        await self.reconcile()

    @maintenance.before_loop
    async def before_maintenance(self):
        await self.bot.wait_until_ready()

    async def stop(self):
        task = self.maintenance.get_task()
        self.maintenance.cancel()
        if task:
            with suppress(asyncio.CancelledError):
                await task
