from fastapi import APIRouter, Depends, Form, Request, Response

from printer_backend.options import ScanOption
from printer_backend.printer import Printer
from printer_backend.printer_repository import PrinterRepository
from printer_backend.scanning import SCAN_FORMATS, source_kind
from printer_backend.settings_repository import SettingsRepository
from printer_backend.storage import DEFAULT_SCAN_NAME
from web_frontend.dependencies import get_repository, get_settings
from web_frontend.device_ids import device_to_id, id_to_device

router = APIRouter()

PRIMARY_OPTIONS = ('mode', 'resolution')    # shown first, the others are under "More options"
LABELS = {'l': 'Left', 't': 'Top', 'x': 'Width', 'y': 'Height'}     # the scan area, as scanimage names it
CHECKING = 'checking'   # the availability of a printer until its check is back


def render(request: Request, template: str, **context):
    return request.app.state.templates.TemplateResponse(request, template, context)


def option_fields(options: dict[str, ScanOption]) -> tuple[list[ScanOption], list[ScanOption]]:
    '''The options the user can change, split into the ones to show first and the rest.'''
    editable = [option for option in options.values() if option.active and option.name != 'source']
    primary = [option for name in PRIMARY_OPTIONS for option in editable if option.name == name]
    return primary, [option for option in editable if option not in primary]


def panel(request: Request, device_id: str, printer: Printer, settings: SettingsRepository, template: str, **context):
    '''What depends on the source of the printer: its options and the scan form.'''
    kind = source_kind(printer.source)
    primary, more = option_fields(printer.current_options)
    return render(request, template, device_id=device_id, printer=printer, kind=kind, primary=primary, more=more,
                  labels=LABELS, formats=SCAN_FORMATS, default_name=DEFAULT_SCAN_NAME,
                  save_location=settings.save_location,
                  # the same defaults as the printer: separate pages one at a time, one document for a stack
                  combine_by_default=kind != 'flatbed', **context)


def refuse_during_scan(request: Request):
    '''Looking for printers and reading their options talk to the devices, which a scan is using.'''
    if request.app.state.scans.busy:
        raise RuntimeError('Not while a scan is running')


@router.get('/')
async def home(request: Request, repository: PrinterRepository = Depends(get_repository),
               settings: SettingsRepository = Depends(get_settings)):
    # the active printer first, and if it was deleted the first saved one takes over
    printers = sorted(repository.get_all_printers(), key=lambda printer: printer.device != settings.active_printer)
    return render(request, 'printers/home.html', checking=CHECKING,
                  printers=[{'id': device_to_id(printer.device), 'device': printer.device} for printer in printers])


@router.post('/printers/active')
async def choose_printer(device_id: str = Form(), repository: PrinterRepository = Depends(get_repository),
                         settings: SettingsRepository = Depends(get_settings)):
    device = id_to_device(device_id)
    repository.get_printer(device)      # raises ValueError if it isn't saved
    settings.active_printer = device
    return Response()                   # empty: clears an earlier message


@router.get('/printers/discover')
async def discover_page(request: Request):
    return render(request, 'printers/discover.html')


@router.post('/printers/discover')
async def discover(request: Request, repository: PrinterRepository = Depends(get_repository)):
    refuse_during_scan(request)
    return render(request, 'printers/_discovered.html', found=await repository.find_new_printers())


@router.post('/printers')
async def add_printer(request: Request, device: str = Form(), repository: PrinterRepository = Depends(get_repository),
                      settings: SettingsRepository = Depends(get_settings)):
    refuse_during_scan(request)
    printer = Printer(device)
    await printer.load_options()
    repository.add_printer(printer)
    settings.active_printer = device    # it was added to be used
    return Response(headers={'HX-Redirect': '/'})


@router.get('/printers/{device_id}')
async def printer_page(request: Request, device_id: str, repository: PrinterRepository = Depends(get_repository),
                       settings: SettingsRepository = Depends(get_settings)):
    printer = repository.get_printer(id_to_device(device_id))
    return panel(request, device_id, printer, settings, 'printers/detail.html', checking=CHECKING)


@router.get('/printers/{device_id}/availability')
async def availability(request: Request, device_id: str, repository: PrinterRepository = Depends(get_repository)):
    printer = repository.get_printer(id_to_device(device_id))
    if request.app.state.scans.busy:
        state = 'in use'    # asking the device now could disturb the scan
    else:
        state = 'available' if await printer.is_available() else 'not reachable'
    return render(request, 'printers/_availability.html', state=state, device_id=device_id, checking=CHECKING)


@router.post('/printers/{device_id}/source')
async def change_source(request: Request, device_id: str, source: str = Form(),
                        repository: PrinterRepository = Depends(get_repository),
                        settings: SettingsRepository = Depends(get_settings)):
    printer = repository.get_printer(id_to_device(device_id))
    printer.select_source(source)
    repository.update_printer(printer)
    return panel(request, device_id, printer, settings, 'printers/_panel.html')


@router.post('/printers/{device_id}/options')
async def change_options(request: Request, device_id: str, repository: PrinterRepository = Depends(get_repository)):
    form = dict(await request.form())
    source = form.pop('source_of_form', None)   # the source the page showed, in case it was changed elsewhere since
    settings = {name: value for name, value in form.items() if value != ''}     # an empty field is left as it is
    repository.update_options(id_to_device(device_id), settings, source)
    return render(request, 'printers/_saved.html')


@router.post('/printers/{device_id}/delete')
async def delete_printer(device_id: str, repository: PrinterRepository = Depends(get_repository)):
    repository.remove_printer(id_to_device(device_id))
    return Response(headers={'HX-Redirect': '/'})
