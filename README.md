# vgmelo-agenda

Booking service behind [vgmelo.tech/agenda](https://vgmelo.tech/agenda): recruiters and clients pick a time to talk
with me, from the site, from a small scheduling agent, or from their own AI agent through a public MCP server.

- **FastAPI + Postgres** (SQLite for tests and local runs), one small container.
- **Outlook busy times** come from the calendar published as ICS ("can view when I'm busy"), refreshed every
  10 minutes. If the feed is stale for 6 hours the agenda closes instead of double booking.
- **E-mail confirmation** (Resend): a request holds the slot for 30 minutes until the guest clicks the link. The
  same link cancels or reschedules (up to 2 hours before). I get the details and an `.ics`, plus my own cancel link.
- **MCP server** at `/api/agenda/mcp` (Streamable HTTP, stateless) with two tools, `horarios_livres` and `marcar`.
- **Scheduling agent** on the page: an LLM (Groq, OpenRouter free models as fallback) that can only call those two
  tools.

```bash
claude mcp add --transport http agenda-victor https://vgmelo.tech/api/agenda/mcp
```

## Rules

Job calls 15–60 min, consulting 30–90 min, in 15-minute steps. Mon–Sat, 08–10, 12–14 and 18–20
(America/Sao_Paulo), 12 hours to 14 days ahead, at most 2 calls a day with 15 minutes between them. All of it is
configurable (see `.env.example`).

## How it stays correct and hard to abuse

- **No double booking under races.** A booking claims its 5-minute cells (time plus buffer) in a table whose primary
  key is the cell, and one unit of its day's counter with a conditional `UPDATE`, in the same transaction. Two
  overlapping requests can both pass the availability check; only one commits. Tested with threads on Postgres
  (and the tests fail if either guard is removed).
- **Same rules for every door.** The web API, the MCP tools and the agent call the same service, with the same
  per-IP limit, one pending request per e-mail, two future calls per e-mail, a global cap on pending holds and a
  daily cap on confirmation e-mails.
- **No leaks.** Availability returns start times only. The guest's e-mail never contains text the guest typed, so
  the form can't be used to send spam from my domain. Tokens travel in the URL fragment and are stored hashed.
- **Strict inputs.** Pydantic models with `extra="forbid"` on the API and on the MCP tools (the SDK ignores unknown
  arguments by default; here they are an error).
- **A narrow agent.** Two tools, a fixed prompt, history kept on the server (the browser sends only its new message),
  short messages, 10 turns per conversation, 4 LLM calls per turn, per-IP limit and a daily token budget. When the
  budget runs out the page shows only the form. Its LLM keys are used by nothing else.

## Run

```bash
uv sync
SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))") DEV_LOG_LINKS=true PORT=8010 uv run vgmelo-agenda
```

`DEV_LOG_LINKS=true` prints the confirmation link to the log when there's no Resend key (local only).

## Test

```bash
uv run pytest
AGENDA_TEST_DATABASE_URL=postgresql+psycopg://user@127.0.0.1:5432/agenda_test uv run pytest  # races on Postgres
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

## License

MIT
