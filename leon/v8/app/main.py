
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from .config import Settings, get_settings
from .database import SessionRepository, get_connection, init_db
from .models import (
    ConversationContract,
    HealthResponse,
    PromptSpec,
    ProposeTicketRequest,
    ProposeTicketResponse,
    StartSessionRequest,
    StartSessionResponse,
    SubmitSessionResponse,
    SuggestFromTextRequest,
    SuggestFromTextResponse,
    TicketDetail,
    TicketSummary,
    UserTurnRequest,
    UserTurnResponse,
)
from .service import IntakeService
from .state_machine import PROMPT_LIBRARY


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    conn = get_connection(settings.database_path)
    init_db(conn)
    app.state.db_conn = conn
    app.state.repository = SessionRepository(conn)
    app.state.service = IntakeService(app.state.repository, settings)
    yield
    conn.close()


app = FastAPI(title="Intake Service", version="0.8.0", lifespan=lifespan)


def get_repository() -> SessionRepository:
    return app.state.repository


def get_service() -> IntakeService:
    return app.state.service


def get_app_settings() -> Settings:
    return get_settings()


RepoDep = Annotated[SessionRepository, Depends(get_repository)]
ServiceDep = Annotated[IntakeService, Depends(get_service)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard", include_in_schema=False)
def dashboard() -> FileResponse:
    template_path = Path(__file__).parent / "templates" / "dashboard.html"
    return FileResponse(template_path)


@app.get("/health", response_model=HealthResponse)
def health(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(status="ok", app_name=settings.app_name, db_path=settings.database_path)


@app.get("/prompt-library", response_model=list[PromptSpec])
def prompt_library() -> list[PromptSpec]:
    return list(PROMPT_LIBRARY.values())


@app.post("/sessions", response_model=StartSessionResponse)
def start_session(body: StartSessionRequest, service: ServiceDep) -> StartSessionResponse:
    session = service.start_session(phone=body.phone, channel=body.channel)
    return StartSessionResponse(
        session_id=session.session_id,
        state=session.state,
        assistant_message=session.last_assistant_message or service.state_machine.start_message(),
        ticket=session.ticket,
        transcript=session.transcript,
        contract=service.build_contract(session),
    )


@app.get("/sessions/{session_id}", response_model=UserTurnResponse)
def get_session(session_id: str, service: ServiceDep) -> UserTurnResponse:
    session = service.get_session_or_404(session_id)
    return UserTurnResponse(
        session_id=session.session_id,
        state=session.state,
        assistant_message=session.last_assistant_message or service.state_machine.start_message(),
        ticket=session.ticket,
        submitted_to_openclaw=session.submitted_to_openclaw,
        openclaw_response=session.openclaw_response,
        transcript=session.transcript,
        missing_fields=service.state_machine.missing_fields(session.ticket),
        contract=service.build_contract(session),
    )


@app.get("/sessions/{session_id}/state", response_model=ConversationContract)
def get_session_contract(session_id: str, service: ServiceDep) -> ConversationContract:
    session = service.get_session_or_404(session_id)
    return service.build_contract(session)


@app.post("/sessions/{session_id}/turn", response_model=UserTurnResponse)
async def user_turn(session_id: str, body: UserTurnRequest, service: ServiceDep) -> UserTurnResponse:
    session = await service.process_turn(session_id, body.user_input)
    return UserTurnResponse(
        session_id=session.session_id,
        state=session.state,
        assistant_message=session.last_assistant_message,
        ticket=session.ticket,
        submitted_to_openclaw=session.submitted_to_openclaw,
        openclaw_response=session.openclaw_response,
        transcript=session.transcript,
        missing_fields=service.state_machine.missing_fields(session.ticket),
        contract=service.build_contract(session),
    )


@app.post("/sessions/{session_id}/suggest", response_model=SuggestFromTextResponse)
def suggest_ticket_fields(session_id: str, body: SuggestFromTextRequest, service: ServiceDep) -> SuggestFromTextResponse:
    return service.suggest_from_text(session_id, body.text)


@app.post("/sessions/{session_id}/propose", response_model=ProposeTicketResponse)
def propose_ticket_fields(session_id: str, body: ProposeTicketRequest, service: ServiceDep) -> ProposeTicketResponse:
    return service.propose_ticket_update(session_id, body)


@app.post("/sessions/{session_id}/submit", response_model=SubmitSessionResponse)
async def submit_session(session_id: str, service: ServiceDep) -> SubmitSessionResponse:
    session, message = await service.submit_session(session_id)
    return SubmitSessionResponse(
        session_id=session.session_id,
        state=session.state,
        ticket=session.ticket,
        submitted_to_openclaw=session.submitted_to_openclaw,
        openclaw_response=session.openclaw_response,
        message=message,
        transcript=session.transcript,
        contract=service.build_contract(session),
    )


@app.get("/tickets", response_model=list[TicketSummary])
def list_tickets(repository: RepoDep, limit: int = 50) -> list[TicketSummary]:
    return repository.list_tickets(limit=limit)


@app.get("/tickets/{ticket_id}", response_model=TicketDetail)
def get_ticket(ticket_id: str, repository: RepoDep) -> TicketDetail:
    detail = repository.get_ticket_detail(ticket_id)
    if not detail:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Ticket not found")
    return detail


@app.get("/followup-actions")
def list_followup_actions(repository: RepoDep, limit: int = 50) -> list[dict[str, Any]]:
    return [action.model_dump(mode="json") for action in repository.list_followup_actions(limit=limit)]


@app.post("/webhooks/twilio/voice/inbound", include_in_schema=True)
async def twilio_voice_inbound(request: Request, service: ServiceDep) -> Response:
    payload = await _read_form_or_json(request)
    result = await service.handle_voice_inbound(payload)
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        f"<Response><Say>{_xml_escape(result['say'])}</Say><Pause length='1'/></Response>"
    )
    return Response(content=xml, media_type="application/xml")


@app.post("/webhooks/twilio/voice/status")
async def twilio_voice_status(request: Request, service: ServiceDep) -> dict[str, Any]:
    payload = await _read_form_or_json(request)
    return await service.handle_voice_status(payload)


@app.post("/webhooks/twilio/sms/inbound")
async def twilio_sms_inbound(request: Request, service: ServiceDep) -> Response:
    payload = await _read_form_or_json(request)
    result = await service.handle_sms_inbound(payload)
    xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        f"<Response><Message>{_xml_escape(result['reply'])}</Message></Response>"
    )
    return Response(content=xml, media_type="application/xml")


async def _read_form_or_json(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        return await request.json()
    form = await request.form()
    return dict(form)


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
