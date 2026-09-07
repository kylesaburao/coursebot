# SFU CourseBot

Discord course commands backed by the [SFU Courses API](https://api.sfucourses.com/), plus mandatory Study Guard role management. Run one process per bot. Study Guard state is in memory and resets when the process restarts.

## Commands

Use either `/` application commands or the `!` prefix. Quote arguments containing spaces in prefix commands.

| Command | Arguments | Behavior |
| --- | --- | --- |
| `course` | `subject course_number` | Course details and four most recent offerings, ordered by year and semester. |
| `offerings` | `instructor_name [term]` | Instructor offerings, with case-insensitive partial term filtering. An exact full-name match takes priority over ambiguity. |
| `section` | `year term dept number` | Sections with all instructors and meeting schedules, including campus. Missing information displays as TBA. |
| `reviews` | `instructor_name` | Review summary for a case-insensitive full-name lookup. |

```text
!course CMPT 105W
!offerings "Brian Fraser" "2025-fall"
!offerings "Brian Fraser" summer
!section 2026 spring CMPT 120
!reviews "Brian Fraser"
```

Departments and section terms are case-insensitive. Section terms must be `spring`, `summer`, or `fall`, with a four-digit year. Course numbers may include letters, such as `105W`. Term filters match the API's year-season text, such as `2025-fall`, `2025`, or `fall`. Leading, trailing, and repeated whitespace is normalized for names and filters. Long lists span consecutive embeds. Unusually long individual values are explicitly marked as truncated.

## Startup

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.template .env
# Fill in .env before starting.
python bot.py
```

Enable **Server Members Intent** and **Message Content Intent** in the Discord developer portal. The bot also uses the default voice-state intent. Invite it with the bot and application-command scopes. Give it View Channel, Send Messages, and Embed Links where commands are used.

Create the Study Time role below the bot's highest role. Give the bot Manage Roles and access to the configured voice and moderation text channels. The moderation channel needs View Channel and Send Messages. Configure the role's chat permissions as desired. Study Guard assigns the role, it does not change channel permission overwrites.

Configuration is read once at startup, including `.env`. Existing process environment variables take precedence. Required settings:

| Environment setting | Meaning |
| --- | --- |
| `DISCORD_TOKEN` | Bot token. |
| `GUILD_ID` | Guild containing all Study Guard objects. |
| `STUDY_TIME_VC_CHANNEL_ID` | Study Time voice channel ID. |
| `MODERATION_REPORT_VC_CHANNEL_ID` | Moderation **text** channel ID, despite the historical environment name. |
| `STUDY_TIME_ROLE_NAME` | Exact name of the assignable Study Time role. |

IDs, counts, and durations must be positive integers. Optional settings retain these defaults:

| Environment setting | Default |
| --- | --- |
| `STUDY_TIME_VC_JOIN_LIMIT_COUNT` | 5 |
| `STUDY_TIME_VC_JOIN_LIMIT_WINDOW_SECONDS` | 60 |
| `STUDY_TIME_VC_SHORT_STAY_SECONDS` | 30 |
| `STUDY_TIME_VC_SHORT_STAY_THRESHOLD` | 5 |
| `STUDY_TIME_VC_SHORT_STAY_WINDOW_SECONDS` | 120 |

Study Guard checks both limits before recording an eligible join. Only eligible joins and observed eligible visits shorter than the short-stay duration count. Entries expire exactly at their window boundary. Rejected joins extend neither history. When both limits apply, the reported wait covers both, rounded up to whole seconds.

A denied occupant must leave and rejoin after the wait to receive the role. Timeout expiry and gateway reconnects do not make that visit eligible. Direct channel moves count as joins and departures. Startup occupants receive the role without fabricated historical arrival times. A process restart resets limits and denied visits.

Readiness, gateway resume, and ten-minute maintenance reconcile role state. Missing objects, permissions, and failed role operations are logged and retried during reconciliation. Failed notifications do not stop role handling.

The process serves `ok` on `/` and `/health`, port 8080, as liveness checks. These endpoints do not verify Discord connectivity or upstream API health. One shared HTTP session uses a 15-second total request timeout. Shutdown closes the session, bot, health runner, and maintenance task. Importing modules does not start the application.

## Verification

```sh
python -m pytest -q
python -m compileall -q bot.py src
python -m pip check
docker build -t coursebot-check .
docker run --rm coursebot-check python -m pytest -q
```

To run tests in reverse collection order:

```sh
python - <<'PY'
import pytest
class Reverse:
    def pytest_collection_modifyitems(self, items):
        items.reverse()
raise SystemExit(pytest.main(['-q'], plugins=[Reverse()]))
PY
```

Tests use real command callbacks, a local HTTP server, and simulated Discord objects. They do not exercise live Discord roles, gateway reconnects, application-command synchronization, or AWS deployment. Docker uses Python 3.13. `docker compose up --build` runs the same single-process application with `.env`.

The existing GitHub workflow tests changes and deploys main-branch pushes through ECR and App Runner. OIDC permission is limited to the deployment job.

`sfucourses-api.json` is the published schema embedded in the API reference page, refreshed on 2026-09-07. Its advertised `/docs/swagger.json` URL returned 404 during the refresh. Actual section responses are arrays of course objects with `sections[].instructors[].name` and `sections[].schedules[]`. The published section response schema contains an extra array nesting. Review summaries use the `/v1/rest/reviews/instructors/{instructor_name}` endpoint.

## Acknowledgements

[SFU Courses API](https://api.sfucourses.com/) by Brian Rahadi. Original CourseBot author: [smehars](https://www.github.com/smehars).
