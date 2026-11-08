import os
import shutil
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile

from printer_backend import printing
from printer_backend.printer import Printer
from printer_backend.printer_repository import PrinterRepository
from printer_backend.printing import PrintCapabilities, check_print_file, ensure_queue, printer_capabilities
from printer_backend.settings_repository import SettingsRepository
from web_frontend.dependencies import get_repository, get_settings
from web_frontend.print_job import PrintJob, store_upload

router = APIRouter()
SIDES_LABELS = {'one-sided': 'One side', 'two-sided-long-edge': 'Two sides, long edge',
                'two-sided-short-edge': 'Two sides, short edge'}
COLOR_LABELS = {'color': 'Colour', 'monochrome': 'Black and white'}
PRINT_REDIRECT = {'HX-Redirect': '/print'}


def render(request: Request, template: str, **context):
    return request.app.state.templates.TemplateResponse(request, template, context)


def render_jobs(request: Request):
    '''The list of print jobs, the same wherever it is shown.'''
    return render(request, 'print/_jobs.html', jobs=request.app.state.prints.jobs,
                  sides_labels=SIDES_LABELS, color_labels=COLOR_LABELS)


def active_printer(repository: PrinterRepository, settings: SettingsRepository) -> Printer | None:
    '''The printer the user chose, or None if none was chosen or it has been deleted since.'''
    device = settings.active_printer
    if device is None:
        return None
    try:
        return repository.get_printer(device)
    except ValueError:
        return None


async def prepare_queue(repository: PrinterRepository, printer: Printer) -> tuple[str, PrintCapabilities]:
    '''The printer's CUPS queue and what it offers. The address it was found at is kept for the next print.'''
    queue, uri = await ensure_queue(printer.device, printer.print_uri)
    if uri != printer.print_uri:
        printer.print_uri = uri
        repository.update_printer(printer)
    return queue, await printer_capabilities(queue)


def choose(value: str, offered: list[str], default: str, what: str) -> str:
    '''The value the form sent, if the printer offers it. An empty value (the field is hidden) is the default.'''
    if value == '':
        return default
    if value not in offered:
        raise ValueError(f"This printer can't do '{value}' {what}")
    return value


def parse_copies(text: str, maximum: int) -> int:
    try:
        copies = int(text)
    except ValueError:
        raise ValueError('The number of copies must be a whole number') from None
    if not 1 <= copies <= maximum:
        raise ValueError(f'The number of copies must be from 1 to {maximum}')
    return copies


@router.get('/print')
async def print_page(request: Request, repository: PrinterRepository = Depends(get_repository),
                     settings: SettingsRepository = Depends(get_settings)):
    printer = active_printer(repository, settings)
    capabilities, problem = None, None
    if printer is not None:
        try:
            _, capabilities = await prepare_queue(repository, printer)
        except RuntimeError as error:       # shown on the page: the printer may just be off
            problem = str(error)
    return render(request, 'print/page.html', printer=printer, capabilities=capabilities, problem=problem,
                  sides_labels=SIDES_LABELS, color_labels=COLOR_LABELS, jobs=request.app.state.prints.jobs)


@router.post('/print')
async def start_print(request: Request, file: UploadFile = File(), copies: str = Form('1'),
                      sides: str = Form(''), color_mode: str = Form(''),
                      repository: PrinterRepository = Depends(get_repository),
                      settings: SettingsRepository = Depends(get_settings)):
    printer = active_printer(repository, settings)
    if printer is None:
        raise ValueError('Choose a printer first, on the printers page')
    copies_number = parse_copies(copies, printing.COPIES_MAX)

    # everything that can be refused about the file is checked before the printer is contacted
    folder = os.path.join(printing.PRINT_FOLDER_DEFAULT, uuid4().hex)
    try:
        path = await store_upload(file, folder, printing.MAX_UPLOAD_BYTES)
        check_print_file(path)
        queue, capabilities = await prepare_queue(repository, printer)
        sides_value = choose(sides, capabilities.sides, capabilities.sides_default, 'on each side')
        color_value = choose(color_mode, capabilities.color_modes, capabilities.color_default, 'as a colour mode')
        copies_number = parse_copies(copies, capabilities.copies_max)
    except BaseException:
        # nothing was accepted, so the file is not kept
        shutil.rmtree(folder, ignore_errors=True)
        raise

    job = PrintJob(os.path.basename(path), folder, path, printer.device, queue,
                   copies_number, sides_value, color_value)
    request.app.state.prints.start(job)
    return Response(headers=PRINT_REDIRECT)


@router.get('/print/status')
async def print_status(request: Request):
    return render_jobs(request)


@router.post('/print/{job_id}/cancel')
async def cancel_print(request: Request, job_id: str):
    job = request.app.state.prints.get(job_id)
    await job.cancel()
    return render_jobs(request)
