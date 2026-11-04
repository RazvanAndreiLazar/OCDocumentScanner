from fastapi import APIRouter, Depends, Form, Request, Response

from printer_backend.printer import Printer
from printer_backend.printer_repository import PrinterRepository
from printer_backend.scanning import source_kind
from printer_backend.settings_repository import SettingsRepository
from printer_backend.storage import DEFAULT_SCAN_NAME, check_scan_name
from web_frontend.dependencies import get_repository, get_settings
from web_frontend.device_ids import id_to_device
from web_frontend.routes.settings import change_save_location
from web_frontend.scan_job import ScanJob

router = APIRouter()
RUNNING_SCAN = {'HX-Redirect': '/scan'}


def render(request: Request, template: str, **context):
    return request.app.state.templates.TemplateResponse(request, template, context)


@router.post('/scan')
async def start_scan(request: Request,
                     device_id: str = Form(),
                     name: str = Form(DEFAULT_SCAN_NAME),
                     output_format: str = Form('pdf'),
                     combine: bool = Form(False),
                     two_sided: bool = Form(False),
                     how: str = Form(''),   # 'one' or 'all', only asked for a source that isn't a known kind
                     repository: PrinterRepository = Depends(get_repository),
                     settings: SettingsRepository = Depends(get_settings)):
    manager = request.app.state.scans
    if manager.busy:
        return Response(headers=RUNNING_SCAN)

    # everything that can be refused is checked before any paper moves
    saved = repository.get_printer(id_to_device(device_id))
    printer = Printer.from_dict(saved.to_dict())    # a copy: changing or deleting the printer can't affect the scan
    name = name.strip() or DEFAULT_SCAN_NAME
    check_scan_name(name)
    # the form sends the folder it shows: leaving that field by pressing this button also saves it,
    # and that request might only arrive after this one
    path = (await request.form()).get('path')
    if path is None or path.strip() == str(settings.save_location):
        await settings.save_location.validate_storage_path()
    else:
        await change_save_location(settings, path)
    printer.to_args()   # validates the saved options

    kind = source_kind(printer.source)
    if kind is None and how not in ('one', 'all'):
        raise ValueError(f"Choose how to scan '{printer.source}'")
    scan = printer.scan(one_at_a_time=(how == 'one') if kind is None else None,
                        combine=combine and output_format == 'pdf',     # only pdf pages can be combined
                        output_format=output_format,
                        two_sided=two_sided and kind == 'document-feeder')
    manager.start(ScanJob(scan, name, settings.save_location, printer.device))
    return Response(headers=RUNNING_SCAN)


@router.get('/scan')
async def scan_page(request: Request):
    return render(request, 'scan/job.html')


@router.get('/scan/banner')
async def scan_banner(request: Request):
    return render(request, 'scan/_banner.html')


@router.get('/scan/status')
async def scan_status(request: Request):
    return render(request, 'scan/_status.html')


def current_job(request: Request) -> ScanJob:
    job = request.app.state.scans.job
    if job is None:
        raise ValueError('There is no scan')
    return job


@router.post('/scan/answer')
async def scan_answer(request: Request, prompt_id: int = Form(), reply: bool = Form(False)):
    current_job(request).answer(prompt_id, reply)   # a question that isn't open anymore is ignored
    return render(request, 'scan/_status.html')


@router.post('/scan/cancel')
async def scan_cancel(request: Request):
    job = current_job(request)
    job.cancel()
    await job.wait()
    return render(request, 'scan/_status.html')


@router.post('/scan/save')
async def scan_retry_save(request: Request):
    current_job(request).retry_save()
    return render(request, 'scan/_status.html')
