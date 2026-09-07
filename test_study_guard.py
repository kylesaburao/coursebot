import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
import pytest

from src.study_guard import StudyGuard, StudyGuardConfig, wait_seconds


def fixture():
    config = StudyGuardConfig(1, 2, 3, 'Study')
    role = Mock(id=4)
    role.name = 'Study'
    role.is_assignable.return_value = True
    channel = Mock(spec=discord.VoiceChannel, id=2, members=[])
    report = Mock(spec=discord.TextChannel)
    report.send = AsyncMock()
    report.permissions_for.return_value = SimpleNamespace(send_messages=True, view_channel=True)
    guild = Mock(id=1, unavailable=False, roles=[role], members=[])
    guild.me.guild_permissions.manage_roles = True
    guild.get_channel.side_effect = lambda channel_id: {2: channel, 3: report}.get(channel_id)
    bot = Mock()
    bot.get_guild.return_value = guild
    bot.wait_until_ready = AsyncMock()
    guard = StudyGuard(bot, config)
    member = Mock(id=10, guild=guild)
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.send = AsyncMock()
    member.get_role.return_value = None
    guild.members = [member]
    return guard, member, channel, report


def voice(channel):
    return SimpleNamespace(channel=channel)


def http_error():
    return discord.HTTPException(SimpleNamespace(status=500, reason='failure'), 'failure')


@pytest.mark.asyncio
@pytest.mark.parametrize('race', [False, True])
async def test_fifth_short_stay_survives_reconciliation(race):
    guard, member, channel, _ = fixture()
    guard.join_history[10] = [10, 30, 50, 70]
    guard.short_stay_history[10] = [11, 31, 51, 71]
    adding, release = asyncio.Event(), asyncio.Event()
    queued, departing = asyncio.Event(), asyncio.Event()
    async def add(*args, **kwargs):
        adding.set()
        await release.wait()
    async def reconcile():
        queued.set()
        await guard.reconcile()
    async def ready():
        departing.set()
    member.add_roles.side_effect = add
    with patch('src.study_guard.monotonic', return_value=100) as clock:
        channel.members = [member]
        join = asyncio.create_task(guard.on_voice_state_update(member, voice(None), voice(channel)))
        await adding.wait()
        tasks = [join]
        channel.members = []
        if race:
            tasks.append(asyncio.create_task(reconcile()))
            await queued.wait()
        clock.return_value = 102
        guard.bot.wait_until_ready.side_effect = ready
        tasks.append(asyncio.create_task(guard.on_voice_state_update(member, voice(channel), voice(None))))
        await departing.wait()
        pending = guard.pending_voice_events
        release.set()
        await asyncio.gather(*tasks)
        assert pending == 2
        assert guard.short_stay_history[10] == [11, 31, 51, 71, 102]
        assert guard.pending_voice_events == 0
        clock.return_value = 103
        channel.members = [member]
        await guard.on_voice_state_update(member, voice(None), voice(channel))
    assert 10 in guard.denied_visits and 10 not in guard.active_visits
    assert member.add_roles.await_count == 1
    assert member.remove_roles.await_count == 2
    member.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_departure_before_reconciliation_snapshot_preserves_short_stay():
    guard, member, channel, _ = fixture()
    guard.join_history[10] = [10, 30, 50, 70]
    guard.short_stay_history[10] = [11, 31, 51, 71]
    adding, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    departure = None
    def gateway_departure():
        nonlocal departure
        channel.members = []
        clock.return_value = 102
        departure = asyncio.create_task(guard.on_voice_state_update(member, voice(channel), voice(None)))
    async def add(*args, **kwargs):
        adding.set()
        await release.wait()
        loop.call_soon(gateway_departure)
    async def reconcile():
        queued.set()
        await guard.reconcile()
    member.add_roles.side_effect = add
    with patch('src.study_guard.monotonic', return_value=100) as clock:
        channel.members = [member]
        join = asyncio.create_task(guard.on_voice_state_update(member, voice(None), voice(channel)))
        await adding.wait()
        reconciliation = asyncio.create_task(reconcile())
        await queued.wait()
        release.set()
        await asyncio.gather(join, reconciliation)
        assert departure is not None
        await departure
        assert guard.short_stay_history[10] == [11, 31, 51, 71, 102]
        clock.return_value = 103
        channel.members = [member]
        await guard.on_voice_state_update(member, voice(None), voice(channel))
    assert 10 in guard.denied_visits and 10 not in guard.active_visits
    member.add_roles.assert_awaited_once()
    assert guard.pending_voice_events == 0


@pytest.mark.asyncio
async def test_departure_after_reconciliation_snapshot_preserves_short_stay():
    guard, member, channel, _ = fixture()
    guard.active_visits[10] = 100
    guard.short_stay_history[10] = [11, 31, 51, 71]
    channel.members = [member]
    member.get_role.return_value = member.guild.roles[0]
    loop = asyncio.get_running_loop()
    departure = None
    def gateway_departure():
        nonlocal departure
        channel.members = []
        clock.return_value = 102
        departure = asyncio.create_task(guard.on_voice_state_update(member, voice(channel), voice(None)))
    resolve = guard.resolve
    def resolve_then_depart():
        guild = resolve()
        loop.call_soon(gateway_departure)
        return guild
    with patch('src.study_guard.monotonic', return_value=102) as clock:
        with patch.object(guard, 'resolve', side_effect=resolve_then_depart):
            await guard.reconcile()
        await asyncio.sleep(0)
        assert departure is not None
        await departure
    assert guard.short_stay_history[10] == [11, 31, 51, 71, 102]
    assert not guard.active_visits
    assert guard.pending_voice_events == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('waiting_for', ['readiness', 'lock'])
async def test_cancelled_voice_event_allows_reconciliation(waiting_for):
    guard, member, channel, _ = fixture()
    guard.active_visits[10] = 100
    guard.join_history[10] = [1]
    entered, ready = asyncio.Event(), asyncio.Event()
    async def wait_until_ready():
        entered.set()
        await ready.wait()
    guard.bot.wait_until_ready.side_effect = wait_until_ready
    if waiting_for == 'lock':
        ready.set()
        await guard.lock.acquire()
    with patch('src.study_guard.monotonic', return_value=102):
        event = asyncio.create_task(guard.on_voice_state_update(member, voice(channel), voice(None)))
        await entered.wait()
        try:
            assert guard.pending_voice_events == 1
        finally:
            event.cancel()
            with pytest.raises(asyncio.CancelledError):
                await event
            if waiting_for == 'lock':
                guard.lock.release()
        assert guard.pending_voice_events == 0
        assert guard.active_visits == {10: 100}
        await guard.reconcile()
    assert not guard.active_visits and not guard.join_history
    member.remove_roles.assert_not_awaited()


def test_exact_windows_combined_wait_and_rejected_history():
    guard, member, _, _ = fixture()
    guard.config = replace(guard.config, join_limit_count=2, short_stay_threshold=2)
    guard.join_history[10] = [0, 1]
    guard.short_stay_history[10] = [0, 2]
    assert guard.join(10, 59.1) == 61
    assert guard.join_history[10] == [0, 1]
    assert guard.short_stay_history[10] == [0, 2]
    guard.join(10, 122)
    assert 10 in guard.denied_visits and 10 not in guard.active_visits
    guard.leave(10, 122)
    assert guard.join(10, 122) == 0
    assert guard.join_history[10] == [122]
    assert wait_seconds([0, 1], 2, 60, 60) == 0


def test_short_stays_and_pruning_keep_active_timing():
    guard, _, _, _ = fixture()
    guard.join(10, 0)
    guard.leave(10, 30)
    assert 10 not in guard.short_stay_history
    guard.join(10, 31)
    guard.leave(10, 32)
    assert guard.short_stay_history[10] == [32]
    guard.join(10, 33)
    guard.prune(1000)
    assert not guard.join_history and not guard.short_stay_history
    assert guard.active_visits[10] == 33
    guard.leave(10, 1001)
    assert not guard.active_visits


@pytest.mark.asyncio
async def test_moves_ignore_unrelated_and_serialized_role_operations():
    guard, member, channel, _ = fixture()
    other = Mock(id=20)
    entered, release = asyncio.Event(), asyncio.Event()
    operations = []
    async def add(*args, **kwargs):
        entered.set()
        await release.wait()
        operations.append('add')
    async def remove(*args, **kwargs):
        operations.append('remove')
    member.add_roles.side_effect = add
    member.remove_roles.side_effect = remove
    with patch('src.study_guard.monotonic', side_effect=[10, 11, 12, 13]):
        first = asyncio.create_task(guard.on_voice_state_update(member, voice(other), voice(channel)))
        await entered.wait()
        second = asyncio.create_task(guard.on_voice_state_update(member, voice(channel), voice(other)))
        await asyncio.sleep(0)
        assert not member.remove_roles.called
        release.set()
        await asyncio.gather(first, second)
        await guard.on_voice_state_update(member, voice(other), voice(other))
        await guard.on_voice_state_update(member, voice(other), voice(None))
    assert operations == ['add', 'remove']
    assert guard.short_stay_history[10] == [11]
    assert not guard.active_visits
    member.guild = Mock(id=999)
    await guard.on_voice_state_update(member, voice(None), voice(channel))
    assert member.add_roles.await_count == 1


@pytest.mark.asyncio
async def test_denied_reconnect_expiry_and_notifications_fail_independently():
    guard, member, channel, report = fixture()
    guard.config = replace(guard.config, join_limit_count=1)
    guard.join_history[10] = [0]
    member.send.side_effect = http_error()
    report.send.side_effect = http_error()
    with patch('src.study_guard.monotonic', return_value=1):
        await guard.on_voice_state_update(member, voice(None), voice(channel))
    member.remove_roles.assert_awaited_once()
    report.send.assert_awaited_once()
    channel.members = [member]
    with patch('src.study_guard.monotonic', return_value=1000):
        await guard.reconcile()
        await guard.reconcile()
    assert 10 in guard.denied_visits
    member.add_roles.assert_not_awaited()
    await guard.on_voice_state_update(member, voice(channel), voice(None))
    await guard.on_voice_state_update(member, voice(None), voice(channel))
    member.add_roles.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_occupants_stale_roles_retry_and_departed_state():
    guard, member, channel, _ = fixture()
    channel.members = [member]
    member.add_roles.side_effect = http_error()
    await guard.reconcile()
    assert guard.active_visits == {10: None} and not guard.join_history
    member.add_roles.side_effect = None
    await guard.reconcile()
    assert member.add_roles.await_count == 2
    guard.leave(10, 100)
    assert not guard.short_stay_history
    channel.members = []
    member.get_role.return_value = guard.role
    await guard.reconcile()
    member.remove_roles.assert_awaited_once()
    guard.join_history[10] = [100]
    guard.active_visits[10] = 100
    guard.denied_visits.add(10)
    member.guild.members = []
    await guard.reconcile()
    assert not guard.active_visits and not guard.denied_visits and not guard.join_history


@pytest.mark.asyncio
async def test_failed_event_role_change_keeps_eligibility_and_resolution_retries():
    guard, member, channel, _ = fixture()
    member.add_roles.side_effect = http_error()
    await guard.on_voice_state_update(member, voice(None), voice(channel))
    assert 10 in guard.active_visits and 10 not in guard.denied_visits
    member.guild.me.guild_permissions.manage_roles = False
    await guard.reconcile()
    assert guard.role is None
    member.guild.me.guild_permissions.manage_roles = True
    channel.members = [member]
    member.add_roles.side_effect = None
    await guard.reconcile()
    assert guard.role is not None and member.add_roles.await_count == 2


@pytest.mark.asyncio
async def test_independent_instances_and_maintenance_stop():
    first, _, _, _ = fixture()
    second, _, _, _ = fixture()
    first.join(10, 0)
    assert not second.active_visits and first.lock is not second.lock
    ready = asyncio.Event()
    first.bot.wait_until_ready.side_effect = ready.wait
    first.reconcile = AsyncMock()
    first.maintenance.start()
    task = first.maintenance.get_task()
    await asyncio.sleep(0)
    first.reconcile.assert_not_awaited()
    await first.stop()
    assert task.done() and task.cancelled()


@pytest.mark.asyncio
async def test_unavailable_objects_preserve_state_and_retry():
    guard, member, channel, report = fixture()
    guard.join(10, 1)
    member.guild.unavailable = True
    await guard.reconcile()
    assert guard.active_visits == {10: 1}
    member.guild.unavailable = False
    channel.permissions_for.return_value = SimpleNamespace(view_channel=False)
    await guard.reconcile()
    assert guard.channel is None and guard.active_visits == {10: 1}
    channel.permissions_for.return_value = SimpleNamespace(view_channel=True)
    channel.members = [member]
    await guard.reconcile()
    member.add_roles.assert_awaited_once()
    assert guard.active_visits == {10: 1}
