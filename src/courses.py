"""Course commands and the SFU Courses HTTP boundary."""
import asyncio
import json
import logging
import re
from urllib.parse import quote

import aiohttp
import discord
from yarl import URL
from discord.ext import commands

log = logging.getLogger(__name__)


class CourseError(commands.CommandError):
    """An expected input or service error suitable for the user."""


def normalized(value):
    return ' '.join(value.split())


def course_query(dept, number):
    dept, number = dept.strip().lower(), number.strip().upper()
    if not re.fullmatch(r'[a-z]+', dept):
        raise CourseError('Use a department code such as CMPT.')
    if not re.fullmatch(r'[0-9]+[A-Z]*', number):
        raise CourseError('Use a course number such as 101 or 105W.')
    return {'dept': dept, 'number': number}


def value(item, key, default='TBA'):
    result = item.get(key)
    if result is None or result == '':
        return default
    if not isinstance(result, (str, int, float)) or isinstance(result, bool):
        raise CourseError('The course service returned invalid data.')
    return clipped(str(result), 900)


def records(data):
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise CourseError('The course service returned invalid data.')
    return data


def optional_records(item, key):
    data = item.get(key)
    return records([] if data is None else data)


def clipped(text, limit):
    return text if len(text) <= limit else text[:limit - 14] + '… [truncated]'


def text_pages(lines, limit=4000):
    """Preserve every list entry, bounding unusually long individual lines."""
    page = ''
    for line in lines:
        line = clipped(line, limit)
        if page and len(page) + len(line) + 1 > limit:
            yield page
            page = ''
        page = f'{page}\n{line}' if page else line
    if page:
        yield page


def term_key(offering):
    term = value(offering, 'term', '').lower()
    year = re.search(r'[0-9]{4}', term)
    season = next((rank for name, rank in [('fall', 3), ('summer', 2), ('spring', 1)] if name in term), 0)
    return (int(year[0]) if year else 0, season)


class Courses(commands.Cog):
    def __init__(self, session, base_url='https://api.sfucourses.com'):
        self.session = session
        self.base_url = base_url

    async def request(self, path, params=None):
        try:
            async with self.session.get(URL(self.base_url + path, encoded=True), params=params) as response:
                body = await response.read()
                if response.status == 404:
                    raise CourseError('No matching data found.')
                if 400 <= response.status < 500 and response.status != 429:
                    raise CourseError('The course service rejected that query. Check the arguments.')
                if response.status != 200:
                    raise CourseError('The course service is unavailable. Try again later.')
                return json.loads(body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            log.warning('Course request failed: %s %s', path, error)
            raise CourseError('The course service could not be reached. Try again later.') from error
        except (ValueError, UnicodeDecodeError) as error:
            log.warning('Invalid JSON from %s: %s', path, error)
            raise CourseError('The course service returned invalid data.') from error
        except CourseError:
            log.warning('Course request rejected: %s status=%s', path, response.status)
            raise

    async def send_pages(self, ctx, title, lines):
        for page in text_pages(lines):
            await ctx.send(embed=discord.Embed(title=clipped(title, 256), description=page, color=discord.Color.blue()))

    async def cog_command_error(self, ctx, error):
        original = getattr(error, 'original', error)
        if isinstance(original, CourseError):
            log.warning('Course command failed: %s', original)
            await ctx.send(str(original))
        elif isinstance(original, (commands.UserInputError, discord.app_commands.TransformerError)):
            await ctx.send(f'Check the arguments. Usage: {ctx.prefix}{ctx.command.name} {ctx.command.signature}. Quote names containing spaces in prefix commands.')
        else:
            log.error('Unexpected course command failure', exc_info=(type(original), original, original.__traceback__))
            await ctx.send('An unexpected error occurred while handling that command.')

    @commands.hybrid_command(name='course', description='Get detailed info about a course')
    async def course(self, ctx: commands.Context, subject: str, course_number: str):
        query = course_query(subject, course_number)
        await ctx.defer()
        data = records(await self.request('/v1/rest/outlines', query))
        if not data:
            raise CourseError('No matching course found.')
        item = data[0]
        lines = [value(item, 'description'), f"Credits: {value(item, 'units')}", f"Prerequisites: {value(item, 'prerequisites', 'None')}", '**Recent offerings**']
        for offering in sorted(optional_records(item, 'offerings'), key=term_key, reverse=True)[:4]:
            names = offering.get('instructors') or []
            if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
                raise CourseError('The course service returned invalid data.')
            lines.append(f"{value(offering, 'term')}: {clipped(', '.join(names), 900) or 'TBA'}")
        await self.send_pages(ctx, f"{value(item, 'dept')} {value(item, 'number')}: {value(item, 'title')}", lines)

    @commands.hybrid_command(name='offerings', description='Get course offerings by instructor')
    async def offerings(self, ctx: commands.Context, instructor_name: str, term: str = None):
        name = normalized(instructor_name)
        if not name:
            raise CourseError('Enter an instructor name.')
        await ctx.defer()
        data = records(await self.request('/v1/rest/instructors', {'name': instructor_name.strip()}))
        exact = [item for item in data if normalized(value(item, 'name')).casefold() == name.casefold()]
        data = exact or data
        if not data:
            raise CourseError('No matching instructor found.')
        if len(data) > 1:
            await self.send_pages(ctx, 'Multiple instructors found, use a full name', [value(item, 'name') for item in data])
            return
        item = data[0]
        offerings = sorted(optional_records(item, 'offerings'), key=term_key, reverse=True)
        if term:
            term = normalized(term).casefold()
            offerings = [row for row in offerings if term in normalized(value(row, 'term')).casefold()]
        lines = [f"{value(row, 'dept')} {value(row, 'number')}: {value(row, 'title')} ({value(row, 'term')})" for row in offerings]
        await self.send_pages(ctx, f"Courses taught by {value(item, 'name')}", lines or ['No offerings found for this search.'])

    @commands.hybrid_command(name='section', description='Get course sections for a year and term')
    async def section(self, ctx: commands.Context, year: int, term: str, dept: str, number: str):
        query = course_query(dept, number)
        term = term.strip().lower()
        if not 1000 <= year <= 9999 or term not in ('spring', 'summer', 'fall'):
            raise CourseError('Use a four-digit year and spring, summer, or fall.')
        query['term'] = f'{year}-{term}'
        await ctx.defer()
        data = records(await self.request('/v1/rest/sections', query))
        if not data:
            raise CourseError('No matching sections found.')
        for item in data:
            lines = [f"Units: {value(item, 'units')}"]
            for section in optional_records(item, 'sections'):
                names = [value(person, 'name') for person in optional_records(section, 'instructors')]
                lines.append(f"**Section {value(section, 'section')}**")
                lines.extend(f'Instructor: {name}' for name in names or ['TBA'])
                schedules = optional_records(section, 'schedules')
                lines.extend(f"Schedule: {value(row, 'days')} {value(row, 'startTime')}-{value(row, 'endTime')}, {value(row, 'campus')}" for row in schedules)
                if not schedules:
                    lines.append('Schedule: TBA')
            if len(lines) == 1:
                lines.append('No sections available.')
            await self.send_pages(ctx, f"{value(item, 'dept')} {value(item, 'number')}: {value(item, 'title')} ({year}-{term})", lines)

    @commands.hybrid_command(name='reviews', description='Get reviews for an instructor by full name')
    async def reviews(self, ctx: commands.Context, instructor_name: str):
        name = normalized(instructor_name)
        if not name:
            raise CourseError('Enter the instructor’s full name.')
        await ctx.defer()
        item = await self.request('/v1/rest/reviews/instructors/' + quote(name, safe=''))
        if not isinstance(item, dict) or not isinstance(item.get('professor_name'), str):
            raise CourseError('The course service returned invalid data.')
        if normalized(item['professor_name']).casefold() != name.casefold():
            raise CourseError('No reviews found for that full name.')
        await self.send_pages(ctx, f"Reviews for {value(item, 'professor_name')}", [
            f"Department: {value(item, 'department')}", f"Rating: {value(item, 'overall_rating')}/5",
            f"Difficulty: {value(item, 'difficulty_level')}/5", f"Ratings: {value(item, 'total_ratings')}",
            f"Would take again: {value(item, 'would_take_again')}%",
        ])
