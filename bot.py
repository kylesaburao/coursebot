"""Explicit configuration, startup, and resource ownership for CourseBot."""
import asyncio
from dataclasses import dataclass
import logging
import os

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands
from dotenv import load_dotenv

from src.courses import Courses
from src.study_guard import StudyGuard, StudyGuardConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    token: str
    study_guard: StudyGuardConfig


def load_config():
    load_dotenv()

    def required(name):
        result = os.getenv(name, '').strip()
        if not result:
            raise ValueError(f'{name} is required')
        return result

    def positive(name, default=None):
        raw = os.getenv(name, str(default) if default is not None else '')
        try:
            result = int(raw)
        except ValueError:
            raise ValueError(f'{name} must be a positive integer') from None
        if result <= 0:
            raise ValueError(f'{name} must be a positive integer')
        return result

    return Config(required('DISCORD_TOKEN'), StudyGuardConfig(
        guild_id=positive('GUILD_ID'),
        voice_channel_id=positive('STUDY_TIME_VC_CHANNEL_ID'),
        moderation_channel_id=positive('MODERATION_REPORT_VC_CHANNEL_ID'),
        role_name=required('STUDY_TIME_ROLE_NAME'),
        join_limit_count=positive('STUDY_TIME_VC_JOIN_LIMIT_COUNT', 5),
        join_window_seconds=positive('STUDY_TIME_VC_JOIN_LIMIT_WINDOW_SECONDS', 60),
        short_stay_seconds=positive('STUDY_TIME_VC_SHORT_STAY_SECONDS', 30),
        short_stay_threshold=positive('STUDY_TIME_VC_SHORT_STAY_THRESHOLD', 5),
        short_stay_window_seconds=positive('STUDY_TIME_VC_SHORT_STAY_WINDOW_SECONDS', 120),
    ))


class CourseBot(commands.Bot):
    def __init__(self, config, session):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix='!', intents=intents, case_insensitive=True)
        self.config = config
        self.session = session
        self.study_guard = StudyGuard(self, config.study_guard)

    async def setup_hook(self):
        await self.add_cog(Courses(self.session))
        await self.add_cog(self.study_guard)
        synced = await self.tree.sync()
        log.info('Synchronized %s application commands', len(synced))
        self.study_guard.maintenance.start()

    async def on_ready(self):
        log.info('Bot ready as %s', self.user)
        await self.study_guard.reconcile()

    async def on_resumed(self):
        log.info('Discord session resumed')
        await self.study_guard.reconcile()

    async def close(self):
        try:
            await self.study_guard.stop()
        finally:
            await super().close()


async def health_check(request):
    return web.Response(text='ok')


def health_app():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    return app


async def main():
    config = load_config()
    runner = web.AppRunner(health_app())
    try:
        await runner.setup()
        await web.TCPSite(runner, '0.0.0.0', 8080).start()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with CourseBot(config, session) as bot:
                await bot.start(config.token)
    finally:
        await runner.cleanup()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
