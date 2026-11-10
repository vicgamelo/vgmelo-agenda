"""The two scheduling tools, shared by the public MCP server and the site's scheduling agent.

Arguments are validated by Pydantic models with ``extra="forbid"``: an unknown argument is an error, not
something silently dropped. Results never include other people's bookings: only open start times.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools import Tool
from mcp_types import ToolAnnotations
from pydantic import ConfigDict, EmailStr, Field

from . import __version__, rules
from .booking import Agenda, BookingError, BookingRequest, Source

# Set by the HTTP guard for every request, read by tools (MCP calls carry no other notion of who's asking).
CLIENT_IP: ContextVar[str] = ContextVar("client_ip", default="unknown")

KIND_IN = {"vaga": "job", "consultoria": "consulting"}
KIND_OUT = {v: k for k, v in KIND_IN.items()}
DIAS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
_TZ_RE = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+\-]+){0,2}$|^UTC$")

INSTRUCTIONS = """\
Agenda de Victor Melo (MLOps/ML). Recrutadores e clientes marcam conversa por aqui.
Tipos: "vaga" (15 a 60 min) e "consultoria" (30 a 90 min), em passos de 15 min.
Atendimento seg a sáb, 08-10h, 12-14h e 18-20h (America/Sao_Paulo); de 12 horas a 14 dias de antecedência.
Fluxo: horarios_livres -> confirmar com a pessoa tipo, dia, hora, duração, nome, e-mail e assunto -> marcar.
A reserva só vale depois que a pessoa clica no link enviado ao e-mail dela (em até 30 minutos).
"""

ERRORS = {
    "slot_unavailable": "Esse horário não está livre (ou fora das regras). Consulte horarios_livres de novo.",
    "slot_taken": "Alguém acabou de pegar esse horário. Consulte horarios_livres de novo.",
    "day_full": "Esse dia já atingiu o limite de conversas. Escolha outro dia.",
    "invalid_duration": "Duração inválida: vaga 15 a 60 min, consultoria 30 a 90 min, em passos de 15.",
    "invalid_kind": "Tipo inválido: use vaga ou consultoria.",
    "pending_exists": "Já existe um pedido aguardando confirmação para esse e-mail. Confirme pelo link enviado.",
    "too_many_bookings": "Esse e-mail já tem o máximo de conversas futuras marcadas.",
    "agenda_busy": "Muitos pedidos aguardando confirmação agora. Tente de novo em alguns minutos.",
    "calendar_unavailable": "A agenda está temporariamente indisponível. Tente mais tarde.",
    "rate_limited": "Muitas tentativas a partir deste endereço. Tente mais tarde.",
    "email_limit": "Limite de e-mails de confirmação atingido por hoje. Tente amanhã.",
    "email_failed": "Não foi possível enviar o e-mail de confirmação. Tente mais tarde.",
    "email_unavailable": "O envio de e-mails está desligado; não é possível marcar agora.",
}


def tz_or_error(name: str | None, default: ZoneInfo) -> ZoneInfo:
    if not name:
        return default
    if len(name) > 64 or not _TZ_RE.match(name):
        raise ToolError("fuso inválido: use um nome IANA, por exemplo America/Sao_Paulo ou Europe/Lisbon")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ToolError("fuso inválido: use um nome IANA, por exemplo America/Sao_Paulo") from exc


def valid_tz_name(name: str | None) -> str | None:
    """For the web API: a tz name we can store, or None."""
    if not name or len(name) > 64 or not _TZ_RE.match(name):
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


Tipo = Literal["vaga", "consultoria"]
Duracao = Annotated[int, Field(ge=15, le=90, description="Duração em minutos, múltiplo de 15.")]
Fuso = Annotated[str, Field(max_length=64, description="Fuso IANA de quem vai conversar, ex. America/Sao_Paulo.")]


def make_tools(agenda: Agenda, source: Source, ip: Callable[[], str] = CLIENT_IP.get) -> list[Tool]:
    settings = agenda.settings

    def horarios_livres(
        tipo: Tipo,
        duracao_min: Duracao,
        fuso: Fuso = settings.timezone,
        data_inicial: Annotated[
            date | None, Field(description="AAAA-MM-DD; padrão: o primeiro dia com horário.")
        ] = None,
        dias: Annotated[int, Field(ge=1, le=7, description="Quantos dias a partir de data_inicial.")] = 3,
    ) -> dict[str, Any]:
        """Horários de início livres para uma conversa com Victor, agrupados por dia, no fuso pedido."""
        tz = tz_or_error(fuso, settings.tz)
        kind = KIND_IN[tipo]
        try:
            starts = agenda.free_starts(kind, duracao_min)
        except BookingError as exc:
            raise ToolError(ERRORS.get(exc.code, exc.code)) from exc
        # Without a date, start at the first day that has anything open (not "today", often empty).
        first = data_inicial or (starts[0].astimezone(tz).date() if starts else agenda.clock().astimezone(tz).date())
        last = first + timedelta(days=dias - 1)
        days: dict[str, list[str]] = {}
        for s in starts:
            local = s.astimezone(tz)
            if first <= local.date() <= last:
                days.setdefault(local.date().isoformat(), []).append(local.strftime("%H:%M"))
        out = [
            {"data": d, "dia_semana": DIAS[date.fromisoformat(d).weekday()], "inicios": hs} for d, hs in days.items()
        ]
        _, horizon = rules.window_dates(settings, agenda.clock())
        return {
            "tipo": tipo,
            "duracao_min": duracao_min,
            "fuso": str(tz),
            "dias": out,
            "ultimo_dia_aberto": horizon.isoformat(),
            "observacao": "Sem horários no período? Tente outra data_inicial." if not out else "",
        }

    def marcar(
        tipo: Tipo,
        data: Annotated[date, Field(description="AAAA-MM-DD, no fuso informado.")],
        hora: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", description="HH:MM, no fuso informado.")],
        duracao_min: Duracao,
        nome: Annotated[str, Field(min_length=1, max_length=100)],
        email: Annotated[EmailStr, Field(max_length=254)],
        assunto: Annotated[str, Field(min_length=3, max_length=1000, description="Pauta da conversa.")],
        fuso: Fuso = settings.timezone,
        empresa: Annotated[str | None, Field(max_length=100)] = None,
        idioma: Literal["pt", "en"] = "pt",
    ) -> dict[str, Any]:
        """Pede uma conversa com Victor. Fica pendente até a pessoa clicar no link enviado ao e-mail dela."""
        tz = tz_or_error(fuso, settings.tz)
        start = datetime.combine(data, time.fromisoformat(hora), tz)
        req = BookingRequest(
            kind=KIND_IN[tipo],
            start=start,
            duration=duracao_min,
            name=nome,
            email=str(email),
            topic=assunto,
            company=empresa,
            lang=idioma,
            guest_tz=str(tz),
        )
        try:
            b = agenda.create(req, ip(), source)
        except BookingError as exc:
            raise ToolError(ERRORS.get(exc.code, exc.code)) from exc
        return {
            "status": "pendente",
            "inicio": b.start_at.astimezone(tz).isoformat(timespec="minutes"),
            "fim": b.end_at.astimezone(tz).isoformat(timespec="minutes"),
            "confirmar_ate": b.hold_until.astimezone(UTC).isoformat(timespec="minutes"),
            "mensagem": (
                f"Enviamos um link de confirmação para {b.email}. A conversa só vale depois do clique, "
                f"em até {settings.hold_minutes} minutos; sem isso o horário é liberado."
            ),
        }

    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False, idempotent_hint=True)
    writes = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
    return [
        strict(Tool.from_function(horarios_livres, title="Horários livres", annotations=read_only)),
        strict(Tool.from_function(marcar, title="Marcar conversa", annotations=writes)),
    ]


def strict(tool: Tool) -> Tool:
    """Reject unknown arguments instead of ignoring them (the SDK's argument model allows extras)."""
    base = tool.fn_metadata.arg_model

    class Strict(base):  # type: ignore[valid-type,misc]
        model_config = ConfigDict(extra="forbid")

    Strict.__name__ = base.__name__
    tool.fn_metadata.arg_model = Strict
    tool.parameters = Strict.model_json_schema(by_alias=True)
    return tool


def build_mcp(agenda: Agenda) -> MCPServer:
    return MCPServer(
        name="vgmelo-agenda",
        title="Agenda de Victor Melo",
        version=__version__,
        instructions=INSTRUCTIONS,
        website_url="https://vgmelo.tech/agenda",
        tools=make_tools(agenda, "mcp"),
    )
