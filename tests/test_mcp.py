"""MCP server: a real MCP client in-process, and JSON-RPC over HTTP through the guard."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.client import Client

from tests.conftest import Env, EnvFactory, brt
from vgmelo_agenda.tools import build_mcp

HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def run(env: Env, fn: Any) -> Any:
    async def go() -> Any:
        async with Client(build_mcp(env.ctx.agenda)) as c:
            return await fn(c)

    return asyncio.run(go())


def payload(result: Any) -> Any:
    assert not result.is_error, result.content
    return result.structured_content or json.loads(result.content[0].text)


MARCAR = {
    "tipo": "vaga",
    "data": "2026-11-10",
    "hora": "08:00",
    "duracao_min": 30,
    "nome": "Agente da Ana",
    "email": "ana@example.com",
    "assunto": "Vaga de MLOps",
}


def test_tools_are_listed_with_strict_schemas(env: Env) -> None:
    tools = run(env, lambda c: c.list_tools()).tools
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"horarios_livres", "marcar"}
    for t in tools:
        assert t.input_schema.get("additionalProperties") is False
    assert by_name["horarios_livres"].annotations.read_only_hint is True
    assert set(by_name["marcar"].input_schema["required"]) >= {"tipo", "data", "hora", "email", "assunto"}


def test_horarios_livres_in_owner_and_guest_timezones(env: Env) -> None:
    async def go(c: Any) -> Any:
        a = await c.call_tool("horarios_livres", {"tipo": "vaga", "duracao_min": 30, "dias": 2})
        b = await c.call_tool(
            "horarios_livres",
            {
                "tipo": "consultoria",
                "duracao_min": 90,
                "fuso": "Europe/Lisbon",
                "data_inicial": "2026-11-10",
                "dias": 1,
            },
        )
        return payload(a), payload(b)

    brt_days, lisbon = run(env, go)
    # Today (Monday) has nothing left with 12h notice, so the default start is Tuesday, the first open day.
    assert [d["data"] for d in brt_days["dias"]] == ["2026-11-10", "2026-11-11"]
    assert brt_days["dias"][0]["inicios"][0] == "08:00" and brt_days["dias"][0]["dia_semana"] == "terça-feira"
    assert lisbon["dias"][0]["inicios"][:3] == ["11:00", "11:15", "11:30"]
    assert lisbon["fuso"] == "Europe/Lisbon"


def test_marcar_creates_a_pending_booking_and_sends_the_link(env: Env) -> None:
    out = payload(run(env, lambda c: c.call_tool("marcar", MARCAR)))
    assert out["status"] == "pendente" and out["inicio"] == "2026-11-10T08:00-03:00"
    assert "ana@example.com" in out["mensagem"]
    assert "Confirme" in env.net.last_to("ana@example.com")["subject"]
    view = env.post("/booking", {"token": env.net.token("ana@example.com")}).json()
    assert view["status"] == "pending"


def test_marcar_with_guest_timezone(env: Env) -> None:
    args = {**MARCAR, "hora": "11:00", "fuso": "Europe/Lisbon"}
    out = payload(run(env, lambda c: c.call_tool("marcar", args)))
    assert out["inicio"] == "2026-11-10T11:00+00:00"
    assert "08:00" not in out["inicio"]
    assert "Europe/Lisbon" in env.net.last_to("ana@example.com")["text"]


def test_marcar_errors_are_explained(env: Env) -> None:
    async def go(c: Any) -> list[Any]:
        return [
            await c.call_tool("marcar", MARCAR),
            await c.call_tool("marcar", {**MARCAR, "email": "bob@example.com"}),  # same slot
            await c.call_tool("marcar", {**MARCAR, "hora": "08:10", "email": "c@example.com"}),
            await c.call_tool("marcar", {**MARCAR, "duracao_min": 75, "email": "d@example.com"}),
            await c.call_tool("marcar", {**MARCAR, "fuso": "Not/AZone", "email": "e@example.com"}),
        ]

    ok, taken, off_grid, duration, tz = run(env, go)
    assert not ok.is_error
    assert taken.is_error and "não está livre" in taken.content[0].text
    assert off_grid.is_error and "não está livre" in off_grid.content[0].text
    assert duration.is_error and "Duração inválida" in duration.content[0].text
    assert tz.is_error and "fuso inválido" in tz.content[0].text


def test_unknown_or_bad_arguments_are_rejected(env: Env) -> None:
    async def go(c: Any) -> list[Any]:
        return [
            await c.call_tool("marcar", {**MARCAR, "status": "confirmed"}),
            await c.call_tool("horarios_livres", {"tipo": "vaga", "duracao_min": 30, "incluir_reservas": True}),
            await c.call_tool("marcar", {**MARCAR, "email": "not-an-email"}),
            await c.call_tool("horarios_livres", {"tipo": "vaga", "duracao_min": 30, "dias": 30}),
        ]

    for r in run(env, go):
        assert r.is_error
    assert env.net.emails == []


def test_results_never_include_other_bookings(env: Env) -> None:
    env.confirm_new(brt(10, 12, 0), "secret.person@example.com", name="Secret Person", topic="Confidential deal")

    async def go(c: Any) -> str:
        r = await c.call_tool("horarios_livres", {"tipo": "vaga", "duracao_min": 30, "dias": 7})
        return json.dumps(payload(r))

    text = run(env, go)
    for leak in ("secret", "Secret", "Confidential"):
        assert leak not in text
    assert "12:00" not in json.loads(text)["dias"][0]["inicios"]


def rpc(method: str, params: dict[str, Any]) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()


def test_http_endpoint_works_without_origin_and_counts_the_callers_ip(make_env: EnvFactory) -> None:
    env = make_env(booking_max_per_ip_hour=1)
    call = rpc("tools/call", {"name": "marcar", "arguments": MARCAR})
    r = env.client.post("/api/agenda/mcp", content=call, headers=HEADERS)
    assert r.status_code == 200 and '"pendente"' in r.text
    # Same IP, second booking: the per-IP limit saw the MCP caller (context var reached the tool).
    second = rpc("tools/call", {"name": "marcar", "arguments": {**MARCAR, "hora": "12:00", "email": "b@example.com"}})
    r = env.client.post("/api/agenda/mcp", content=second, headers=HEADERS)
    assert "Muitas tentativas" in r.text


def test_http_endpoint_rate_limit_and_body_cap(make_env: EnvFactory) -> None:
    env = make_env(mcp_rate_per_minute=1, mcp_burst=2)
    listing = rpc("tools/list", {})
    assert env.client.post("/api/agenda/mcp", content=listing, headers=HEADERS).status_code == 200
    big = env.client.post("/api/agenda/mcp", content=b" " * 20_000, headers=HEADERS)
    assert big.status_code == 413
    assert env.client.post("/api/agenda/mcp", content=listing, headers=HEADERS).status_code == 200
    r = env.client.post("/api/agenda/mcp", content=listing, headers=HEADERS)
    assert r.status_code == 429 and "retry-after" in r.headers


def test_http_endpoint_dns_rebinding_protection(make_env: EnvFactory) -> None:
    env = make_env(mcp_allowed_hosts="vgmelo.tech")
    listing = rpc("tools/list", {})
    assert env.client.post("/api/agenda/mcp", content=listing, headers=HEADERS).status_code == 200
    evil = env.client.post("/api/agenda/mcp", content=listing, headers={**HEADERS, "host": "evil.example"})
    assert evil.status_code in (400, 421)
    cross = env.client.post("/api/agenda/mcp", content=listing, headers={**HEADERS, "origin": "https://evil.example"})
    assert cross.status_code == 403
