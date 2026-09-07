import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
import pytest

from src.courses import Courses, CourseError, course_query, text_pages


@asynccontextmanager
async def server(handler):
    app = web.Application()
    app.router.add_route('GET', '/{path:.*}', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    try:
        await site.start()
        yield f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
    finally:
        await runner.cleanup()


def context():
    return AsyncMock()


def output(ctx):
    return '\n'.join(call.kwargs['embed'].description for call in ctx.send.call_args_list)


@pytest.mark.asyncio
async def test_section_real_shape_and_encoded_query():
    ctx = context()
    async def handler(request):
        ctx.defer.assert_awaited_once()
        assert dict(request.query) == {'dept': 'cmpt', 'number': '105W', 'term': '2026-fall'}
        return web.json_response([{'dept': 'CMPT', 'number': '105W', 'title': 'Test', 'sections': [
            {'section': 'D100', 'instructors': [{'name': 'Jane Doe'}, {'name': 'John Doe'}],
             'schedules': [{'days': 'Mo,We', 'startTime': '10:30', 'endTime': '11:20', 'campus': 'Burnaby'}, {'days': 'Fr', 'campus': 'Surrey'}]},
            {'section': 'D200'}]}])
    async with server(handler) as url, aiohttp.ClientSession() as session:
        cog = Courses(session, url)
        await cog.section.callback(cog, ctx, 2026, ' FALL ', ' CMPT ', '105w')
    text = output(ctx)
    assert all(part in text for part in ['Jane Doe', 'John Doe', 'Mo,We', '10:30-11:20', 'Burnaby', 'Fr', 'Surrey', 'TBA', 'D200'])


@pytest.mark.asyncio
async def test_offerings_exact_match_filter_and_chronology():
    cog = Courses(None)
    rows = [{'dept': 'CMPT', 'number': '105W', 'title': 'Test', 'term': term} for term in ['2025-spring', '2024-fall', '2025-fall', '2025-summer']]
    cog.request = AsyncMock(return_value=[{'name': 'Jane Doe Jr'}, {'name': 'Jane Doe', 'offerings': rows}])
    ctx = context()
    await cog.offerings.callback(cog, ctx, '  jANE   doE ', ' 2025 ')
    assert [output(ctx).index(term) for term in ['2025-fall', '2025-summer', '2025-spring']] == sorted(output(ctx).index(term) for term in ['2025-fall', '2025-summer', '2025-spring'])
    assert '2024' not in output(ctx)
    cog.request.assert_awaited_with('/v1/rest/instructors', {'name': 'jANE doE'})


@pytest.mark.asyncio
async def test_course_recent_order_and_optional_data():
    cog = Courses(None)
    cog.request = AsyncMock(return_value=[{'dept': 'CMPT', 'number': '105W', 'offerings': [{'term': t} for t in ['2026-spring', '2026-fall', '2026-summer']]}])
    ctx = context()
    await cog.course.callback(cog, ctx, 'cmpt', '105w')
    text = output(ctx)
    assert text.index('fall') < text.index('summer') < text.index('spring')
    assert 'None' in text and 'TBA' in text


@pytest.mark.asyncio
async def test_reviews_path_component_and_summary():
    async def handler(request):
        assert request.raw_path.endswith('/a%2Fb%20%26%20c')
        return web.json_response({'professor_name': 'A/B & C', 'overall_rating': 'N/A', 'total_ratings': '12'})
    async with server(handler) as url, aiohttp.ClientSession() as session:
        cog = Courses(session, url)
        ctx = context()
        await cog.reviews.callback(cog, ctx, ' a/b  & c ')
        assert 'N/A/5' in output(ctx) and '12' in output(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body,message', [(404, '{}', 'No matching'), (400, '{}', 'rejected'), (503, '{}', 'unavailable'), (200, 'oops', 'invalid data')])
async def test_http_failure_and_recovery(status, body, message):
    count = 0
    async def handler(request):
        nonlocal count
        count += 1
        return web.Response(status=status, text=body) if count == 1 else web.json_response([])
    async with server(handler) as url, aiohttp.ClientSession() as session:
        cog = Courses(session, url)
        with pytest.raises(CourseError, match=message):
            await cog.request('/test')
        assert await cog.request('/test') == []


@pytest.mark.asyncio
async def test_concurrency_responsiveness_timeout_and_cancellation():
    entered = 0
    both = asyncio.Event()
    release = asyncio.Event()
    async def handler(request):
        nonlocal entered
        entered += 1
        if entered == 2:
            both.set()
        await release.wait()
        return web.json_response([])
    async with server(handler) as url, aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=.2)) as session:
        cog = Courses(session, url)
        first = asyncio.create_task(cog.request('/one'))
        second = asyncio.create_task(cog.request('/two'))
        await asyncio.wait_for(both.wait(), .15)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(CourseError, match='could not be reached'):
            await second
        release.set()
        assert await cog.request('/recovery') == []


@pytest.mark.asyncio
async def test_embed_limits_preserve_lists():
    cog = Courses(None)
    cog.request = AsyncMock(return_value=[{'name': 'X' * 500, 'offerings': [{'title': 'Y' * 8000, 'number': str(i)} for i in range(40)]}])
    ctx = context()
    await cog.offerings.callback(cog, ctx, 'x')
    assert len(ctx.send.call_args_list) > 1
    for call in ctx.send.call_args_list:
        embed = call.kwargs['embed']
        assert len(embed.title) <= 256 and len(embed.description) <= 4096 and len(embed) <= 6000
        assert all(len(field.value) <= 1024 for field in embed.fields)
    assert '[truncated]' in output(ctx)
    assert all(f'TBA {i}:' in output(ctx) for i in range(40))
    assert len(list(text_pages(['x' * 10000]))[0]) <= 4000


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [{}, [None], [{'sections': 'bad'}], [{'sections': [{'instructors': ['wrong']}]}]])
async def test_malformed_section(payload):
    cog = Courses(None)
    cog.request = AsyncMock(return_value=payload)
    with pytest.raises(CourseError, match='invalid data'):
        await cog.section.callback(cog, context(), 2026, 'fall', 'cmpt', '105w')


@pytest.mark.parametrize('dept,number', [('cmpt&bad', '101'), ('cmpt', '-1'), ('cmpt', '101?')])
def test_invalid_course_input(dept, number):
    with pytest.raises(CourseError):
        course_query(dept, number)


@pytest.mark.asyncio
async def test_ambiguity_no_results_and_validation():
    cog = Courses(None)
    cog.request = AsyncMock(return_value=[{'name': 'Jane A'}, {'name': 'Jane B'}])
    ctx = context()
    await cog.offerings.callback(cog, ctx, 'Jane')
    assert 'Jane A' in output(ctx) and 'Jane B' in output(ctx)
    for args in [(999, 'fall', 'cmpt', '101'), (2026, 'winter', 'cmpt', '101')]:
        with pytest.raises(CourseError):
            await cog.section.callback(cog, context(), *args)
    cog.request.return_value = []
    with pytest.raises(CourseError, match='No matching'):
        await cog.course.callback(cog, context(), 'cmpt', '101')


@pytest.mark.asyncio
@pytest.mark.parametrize('slash', [False, True])
async def test_input_errors_reach_user_through_command_dispatch(slash):
    import discord
    from discord.ext import commands
    from unittest.mock import Mock
    async with commands.Bot(command_prefix='!', intents=discord.Intents.none()) as client:
        cog = Courses(None)
        await client.add_cog(cog)
        ctx = context()
        ctx.bot = client
        ctx.command = cog.course
        ctx.prefix = '/' if slash else '!'
        ctx.command_failed = False
        ctx.kwargs = {'subject': 'bad&query', 'course_number': '101'}
        cog.course.prepare = AsyncMock()
        client.dispatch = Mock()
        if slash:
            client.get_context = AsyncMock(return_value=ctx)
            interaction = Mock(client=client)
            await cog.course.app_command._invoke_with_namespace(interaction, Mock())
        else:
            ctx.args = [cog, ctx]
            try:
                await cog.course.invoke(ctx)
            except commands.CommandError as error:
                await cog.course.dispatch_error(ctx, error)
        assert ctx.command_failed
        assert 'department code' in ctx.send.call_args.args[0]
        ctx.defer.assert_not_awaited()
