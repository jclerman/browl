"""Admin routes, under `/{RESERVED_PREFIX}/admin` (DESIGN.md §2.13). Authentication isn't enforced yet (P-22)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from browl.jobs import BACKGROUND_STAGES, Stage
from browl.pages import templates

STATUS_POLL_S = 2
"""How often the status fragment polls itself while a job runs."""

STAGES = (Stage.UPLOADING, *BACKGROUND_STAGES, Stage.READY)

router = APIRouter(prefix="/admin")


@router.get("/status", response_class=HTMLResponse)
async def job_status(request: Request) -> HTMLResponse:
    """Show the load job's stage, start time, and failed stage and reason (A-5, A-6).

    An htmx request gets just the status fragment, for the upload page (P-6) to embed; while a job runs, the fragment
    polls this route and replaces itself. Any other request gets the fragment in a page.
    """
    template = "admin/_job_status.html" if request.headers.get("HX-Request") == "true" else "admin/status.html"
    return templates.TemplateResponse(
        request,
        template,
        {"job": request.app.state.jobs.state, "stages": STAGES, "poll_s": STATUS_POLL_S},
        headers={"Cache-Control": "no-store"},
    )
