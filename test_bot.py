import asyncio
import os
import subprocess
import sys
from unittest.mock import AsyncMock, Mock

import aiohttp
from aiohttp import web
import pytest

import bot
from src.study_guard import StudyGuardConfig


@pytest.mark.parametrize('order', ['bot,src.study_guard', 'src.study_guard,bot'])
def test_imports_have_no_side_effects(order):
    script = '''
import importlib, socket, asyncio, os
from unittest.mock import patch
import dotenv, aiohttp, discord
from aiohttp import web
from discord.ext import commands, tasks

def forbidden(*args, **kwargs):
    raise AssertionError('import side effect')
with patch.object(dotenv, 'load_dotenv', forbidden), patch.object(os, 'getenv', forbidden), patch.object(commands.Bot, '__init__', forbidden), patch.object(aiohttp.ClientSession, '__init__', forbidden), patch.object(socket.socket, 'connect', forbidden), patch.object(asyncio, 'create_task', forbidden), patch.object(tasks.Loop, 'start', forbidden), patch.object(web.AppRunner, 'setup', forbidden), patch.object(web.TCPSite, 'start', forbidden):
    for name in ORDER.split(','):
        importlib.import_module(name)
'''.replace('ORDER', repr(order))
    subprocess.run([sys.executable, '-c', script], check=True, capture_output=True, text=True)


@pytest.fixture
def config_env(monkeypatch):
    monkeypatch.setattr(bot, 'load_dotenv', Mock())
    for key in list(os.environ):
        if key.startswith('STUDY_TIME_') or key in ('GUILD_ID', 'DISCORD_TOKEN', 'MODERATION_REPORT_VC_CHANNEL_ID'):
            monkeypatch.delenv(key)
    for key, value in {'DISCORD_TOKEN': 'test', 'GUILD_ID': '1', 'STUDY_TIME_VC_CHANNEL_ID': '2', 'MODERATION_REPORT_VC_CHANNEL_ID': '3', 'STUDY_TIME_ROLE_NAME': 'Study'}.items():
        monkeypatch.setenv(key, value)


def test_config_defaults_and_independent_units(config_env, monkeypatch):
    config = bot.load_config()
    assert config.study_guard == StudyGuardConfig(1, 2, 3, 'Study')
    monkeypatch.setenv('STUDY_TIME_VC_JOIN_LIMIT_COUNT', '100')
    assert bot.load_config().study_guard.join_limit_count == 100


@pytest.mark.parametrize('setting,value', [('DISCORD_TOKEN', ''), ('GUILD_ID', '-1'), ('STUDY_TIME_VC_CHANNEL_ID', 'x'), ('STUDY_TIME_ROLE_NAME', ' '), ('STUDY_TIME_VC_SHORT_STAY_THRESHOLD', '0'), ('STUDY_TIME_VC_SHORT_STAY_SECONDS', '-1'), ('MODERATION_REPORT_VC_CHANNEL_ID', '0')])
def test_invalid_config_names_setting(config_env, monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValueError, match=setting):
        bot.load_config()


@pytest.mark.asyncio
async def test_setup_registration_sync_only_once_and_close():
    async with aiohttp.ClientSession() as session:
        client = bot.CourseBot(bot.Config('test', StudyGuardConfig(1, 2, 3, 'Study')), session)
        client.tree.sync = AsyncMock(return_value=[])
        ready = asyncio.Event()
        client.wait_until_ready = AsyncMock(side_effect=ready.wait)
        client.study_guard.reconcile = AsyncMock()
        async with client:
            await asyncio.wait_for(client.setup_hook(), 1)
            assert {'course', 'offerings', 'section', 'reviews'} <= {c.name for c in client.commands}
            assert {c.name for c in client.tree.get_commands()} == {'course', 'offerings', 'section', 'reviews'}
            guard = client.study_guard
            guard.join(10, 0)
            task = guard.maintenance.get_task()
            await client.on_ready()
            await client.on_resumed()
            await client.on_ready()
            assert guard.active_visits == {10: 0}
            assert guard.maintenance.get_task() is task
            client.tree.sync.assert_awaited_once()
        assert task.done() and task.cancelled()


@pytest.mark.asyncio
async def test_health_routes():
    runner = web.AppRunner(bot.health_app())
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    try:
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        async with aiohttp.ClientSession() as session:
            for path in ('/', '/health'):
                async with session.get(f'http://127.0.0.1:{port}{path}') as response:
                    assert response.status == 200 and await response.text() == 'ok'
    finally:
        await runner.cleanup()
    assert not site._server.is_serving()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['setup', 'site', 'discord', 'none'])
async def test_main_cleans_partial_startup(monkeypatch, failure):
    monkeypatch.setattr(bot, 'load_config', lambda: bot.Config('test', StudyGuardConfig(1, 2, 3, 'Study')))
    runner = Mock(setup=AsyncMock(), cleanup=AsyncMock())
    site = Mock(start=AsyncMock())
    monkeypatch.setattr(bot.web, 'AppRunner', Mock(return_value=runner))
    monkeypatch.setattr(bot.web, 'TCPSite', Mock(return_value=site))
    client = Mock(start=AsyncMock(), __aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False))
    client.__aenter__.return_value = client
    sessions = []
    def create(config, session):
        sessions.append(session)
        return client
    monkeypatch.setattr(bot, 'CourseBot', create)
    target = {'setup': runner.setup, 'site': site.start, 'discord': client.start}.get(failure)
    if target:
        target.side_effect = RuntimeError('startup failed')
        with pytest.raises(RuntimeError, match='startup failed'):
            await bot.main()
    else:
        await bot.main()
    runner.cleanup.assert_awaited_once()
    assert all(session.closed and session.timeout.total == 15 for session in sessions)
    if sessions:
        client.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_failure_closes_bot_without_starting_maintenance():
    async with aiohttp.ClientSession() as session:
        client = bot.CourseBot(bot.Config('test', StudyGuardConfig(1, 2, 3, 'Study')), session)
        client.tree.sync = AsyncMock(side_effect=RuntimeError('sync failed'))
        with pytest.raises(RuntimeError, match='sync failed'):
            async with client:
                await client.setup_hook()
        assert client.is_closed()
        assert client.study_guard.maintenance.get_task() is None
