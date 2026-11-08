'''
Printing files on a printer, through CUPS. The CUPS scheduler runs in its own container (cups/), and every
command here talks to it through the socket in CUPS_SERVER. Each printer gets a driverless IPP Everywhere
queue, so CUPS converts the file to what the printer takes; LibreOffice turns office documents into PDF first.

UI-agnostic and async, like the rest of printer_backend: errors are ValueError for a value the user got wrong
and RuntimeError for a command that failed.
'''
import asyncio
from asyncio import subprocess
from dataclasses import dataclass
from contextlib import suppress
import hashlib
import os
from pathlib import Path
import re
import shutil
import signal
import tempfile
from urllib.parse import quote
from uuid import uuid4
import zipfile

from printer_backend.scanning import STDERR_KEPT, stop_process
from printer_backend.storage import check_scan_name

PRINT_FOLDER_DEFAULT = './data/prints'  # next to scans: /app/data/prints in Docker
MAX_UPLOAD_BYTES = 50 * 1024 * 1024     # the largest file that is accepted
UPLOAD_CHUNK = 1024 * 1024              # bytes read from an upload at a time
CONVERT_TIMEOUT = 120                   # seconds for LibreOffice to make a PDF
CUPS_TIMEOUT = 30                       # seconds for one lp, lpadmin, lpstat, ipptool (to CUPS) or cancel
IPP_SEARCH_TIMEOUT = 5                  # seconds for ippfind, which waits for the mDNS answers
IPP_TIMEOUT = 10                        # seconds for ipptool, which asks the printer itself
PRINT_POLL_SECONDS = 2                  # how often a job's state is asked for
COPIES_MAX = 99                         # the most copies, also when the queue allows more (CUPS says 9999)
TITLE_LENGTH = 100
QUEUE_PREFIX = 'ocds-'
NO_PRINT_SERVICE = "Could not find this printer's print service on the network"
JOB_STATE_TEST = str(Path(__file__).parent / 'ipp' / 'get-job-state.test')    # ipptool's request for one job

SIDES = ('one-sided', 'two-sided-long-edge', 'two-sided-short-edge')
COLOR_MODES = ('color', 'monochrome')
DRIVERLESS_MODEL = 'IPP Everywhere'    # how the make-and-model of a queue made with -m everywhere ends

# the file types and what they must start with (or contain). Office and text files are converted to PDF first.
PASS_THROUGH = ('.pdf', '.png', '.jpg', '.jpeg')    # CUPS makes these printable itself
OPEN_DOCUMENT_TYPES = {                             # the mimetype entry of an OpenDocument zip
    '.odt': b'application/vnd.oasis.opendocument.text',
    '.ods': b'application/vnd.oasis.opendocument.spreadsheet',
    '.odp': b'application/vnd.oasis.opendocument.presentation',
}
OOXML_TYPES = ('.docx', '.xlsx', '.pptx')           # zip files with a [Content_Types].xml
OLE_TYPES = ('.doc', '.xls', '.ppt')                # the old binary Office formats
TEXT_TYPES = ('.txt', '.rtf')
PRINT_FORMATS = PASS_THROUGH + tuple(OPEN_DOCUMENT_TYPES) + OOXML_TYPES + OLE_TYPES + TEXT_TYPES
MAGIC = {
    '.pdf': (b'%PDF-',),
    '.png': (b'\x89PNG\r\n\x1a\n',),
    '.jpg': (b'\xff\xd8\xff',),
    '.jpeg': (b'\xff\xd8\xff',),
    '.rtf': (b'{\\rtf',),
}
OLE_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
ZIP_MAGIC = b'PK\x03\x04'
MAGIC_SAMPLE = 8192                                 # bytes read to check a file's type

JOB_PENDING = 'pending'         # waiting in the queue (also when the printer is offline)
JOB_PROCESSING = 'processing'   # being printed
JOB_COMPLETED = 'completed'
JOB_CANCELED = 'canceled'
JOB_ABORTED = 'aborted'


@dataclass
class PrintCapabilities():
    '''What a printer offers: the values the form may show, and the one that is selected by default.'''
    sides: list[str]
    sides_default: str
    color_modes: list[str]
    color_default: str
    copies_max: int = COPIES_MAX


@dataclass
class JobState():
    state: str      # one of the JOB_ values
    reason: str     # what CUPS says about it, e.g. 'printer offline'; '' if nothing


async def run_tool(*args: str, timeout: float, check: bool = True) -> str:
    '''
    Runs a command with its arguments as a list (never through a shell) and returns what it printed. With
    check, a command that fails raises RuntimeError with its error output. The command and everything it
    started are killed when the timeout passes or the caller is cancelled.
    '''
    try:
        # a session of its own, so that the whole group can be killed (soffice starts child processes)
        proc = await subprocess.create_subprocess_exec(*args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                       start_new_session=True)
    except FileNotFoundError:
        raise RuntimeError(f"'{args[0]}' was not found, is its package installed?") from None
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        raise RuntimeError(f"'{args[0]}' timed out after {timeout:g} s") from None
    finally:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        await stop_process(proc)
    if check and proc.returncode != 0:
        error = stderr.decode(errors='replace')[:STDERR_KEPT].strip()
        raise RuntimeError(f"'{args[0]}' failed (Err Code: {proc.returncode})\n{error}")
    return stdout.decode(errors='replace')


######################### FILES ######################

def print_file_name(name: str) -> str:
    '''
    The name a file is stored under: its base name only (no folders, so nothing can be written outside
    the job's folder), with characters that are not plain letters, digits or ( ) - _ . replaced, and the
    extension in lower case. Raises ValueError if nothing is left of it.
    '''
    base = re.split(r'[/\\]', name)[-1]
    stem, extension = os.path.splitext(base)
    stem = re.sub(r'[^\w() -]', '_', stem).strip(' ')[:100]
    if not stem:
        raise ValueError("The file needs a name")
    check_scan_name(stem)   # refuses '.' and '..', which the sanitising leaves alone
    return stem + extension.lower()


def _is_zip_with(path: str, entries: tuple[str, ...]) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return all(entry in archive.namelist() for entry in entries)
    except (zipfile.BadZipFile, OSError):
        return False


def _open_document_type(path: str) -> bytes | None:
    '''The mimetype a zip file declares (its first entry, uncompressed), or None if it isn't a zip with one.'''
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.read('mimetype').strip()
    except (KeyError, zipfile.BadZipFile, OSError):
        return None


def content_matches(path: str, extension: str) -> bool:
    '''True if the file starts like a file of that extension. The name is not trusted: the content is checked.'''
    try:
        with open(path, 'rb') as file:
            head = file.read(MAGIC_SAMPLE)
    except OSError:
        return False
    if extension in MAGIC:
        return head.startswith(MAGIC[extension])
    if extension in OLE_TYPES:
        return head.startswith(OLE_MAGIC)
    if extension in OOXML_TYPES:
        return head.startswith(ZIP_MAGIC) and _is_zip_with(path, ('[Content_Types].xml',))
    if extension in OPEN_DOCUMENT_TYPES:
        return head.startswith(ZIP_MAGIC) and _open_document_type(path) == OPEN_DOCUMENT_TYPES[extension]
    if extension == '.txt':
        return b'\0' not in head    # plain text has no binary zeros
    return False


def check_print_file(path: str):
    '''Raises ValueError if the file's extension is not one that can be printed, or its content doesn't match it.'''
    name = os.path.basename(path)
    extension = os.path.splitext(name)[1].lower()
    if extension not in PRINT_FORMATS:
        raise ValueError(f"'{name}' can't be printed: only {', '.join(PRINT_FORMATS)} files are accepted")
    if not content_matches(path, extension):
        raise ValueError(f"'{name}' is not a real {extension} file")


async def convert_for_print(path: str) -> str:
    '''The PDF to print for this file: the file itself if CUPS can print it, else a PDF that LibreOffice makes next to it.'''
    if os.path.splitext(path)[1].lower() in PASS_THROUGH:
        return path
    path = os.path.abspath(path)
    folder = os.path.dirname(path)
    # a profile of its own for every conversion: a stuck or broken one can't block the next
    profile = os.path.join(tempfile.gettempdir(), f'lo-{uuid4().hex}')
    try:
        await run_tool('soffice', '--headless', '--norestore', f'-env:UserInstallation={Path(profile).as_uri()}',
                       '--convert-to', 'pdf', '--outdir', folder, path, timeout=CONVERT_TIMEOUT)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    output = os.path.splitext(path)[0] + '.pdf'
    if not os.path.isfile(output):
        raise RuntimeError(f"'{os.path.basename(path)}' could not be converted to PDF")
    return output


async def clean_prints_folder():
    '''Empties the prints folder (creating it if needed), like Printer.clean_scans_folder does for scans.'''
    os.makedirs(PRINT_FOLDER_DEFAULT, exist_ok=True)
    for entry in os.scandir(PRINT_FOLDER_DEFAULT):
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
        else:
            os.remove(entry.path)


######################### PRINTERS AND QUEUES ######################

def queue_name(device: str) -> str:
    '''The CUPS queue of a device: stable for the device, and made of characters CUPS accepts.'''
    return QUEUE_PREFIX + hashlib.sha1(device.encode()).hexdigest()[:12]


async def _find_airscan_uri(name: str) -> str | None:
    '''The IPP address of the mDNS service with exactly this name, if the network has one.'''
    output = await run_tool('ippfind', '-T', str(IPP_SEARCH_TIMEOUT), '_ipp._tcp', '--exec', 'echo',
                            '{service_name}|{service_uri}', ';', timeout=IPP_SEARCH_TIMEOUT + 10, check=False)
    for line in output.splitlines():
        service, _, uri = line.rpartition('|')
        if service == name and uri:
            return uri.strip()
    return None


async def _candidate_uri(device: str) -> str | None:
    if match := re.match(r'airscan:\w+:(.+)$', device):
        return await _find_airscan_uri(match.group(1))
    if match := re.match(r'(?:hpaio|hp):/.*[?&]ip=([^&\s]+)$', device):
        return f'ipp://{match.group(1)}/ipp/print'
    if match := re.match(r'escl:https?://([^/:]+)(?::\d+)?/?$', device):
        return f'ipp://{match.group(1)}/ipp/print'
    return None


async def resolve_print_uri(device: str) -> str:
    '''
    The IPP address of a device, from its scanner name, and checked with the printer itself. Raises
    RuntimeError if the printer doesn't answer at that address.
    '''
    uri = await _candidate_uri(device)
    if uri is None:
        raise RuntimeError(NO_PRINT_SERVICE)
    try:
        await run_tool('ipptool', '-T', str(IPP_TIMEOUT), uri, 'get-printer-attributes.test', timeout=IPP_TIMEOUT + 5)
    except RuntimeError as error:
        raise RuntimeError(NO_PRINT_SERVICE) from error
    return uri


async def _queue_uri(queue: str) -> str | None:
    output = await run_tool('lpstat', '-v', queue, timeout=CUPS_TIMEOUT, check=False)
    match = re.search(r'^device for [^:]+: (\S+)$', output, re.MULTILINE)
    return match.group(1) if match else None


async def _queue_attributes(queue: str) -> str:
    '''
    What CUPS says about a queue (Get-Printer-Attributes, as `ipptool -tv` prints it). These are the IPP
    names lp takes (sides-supported, print-color-mode-supported...), made from the printer's own attributes.
    lpoptions isn't used: it needs the PPD file, which only root on the CUPS container can read.
    '''
    return await run_tool('ipptool', '-tv', _cups_printer_uri(queue), 'get-printer-attributes.test',
                          timeout=CUPS_TIMEOUT, check=False)


async def _queue_ready(queue: str, uri: str) -> bool:
    '''
    True if the queue prints to that address and is driverless. A queue whose lpadmin failed (the printer was
    off) may still be listed with its address, but as a 'Local Raw Printer' that would send PDFs unconverted.
    '''
    if await _queue_uri(queue) != uri:
        return False
    model = _ipp_value(await _queue_attributes(queue), 'printer-make-and-model') or ''
    return model.endswith(DRIVERLESS_MODEL)


async def _add_queue(queue: str, uri: str):
    # -m everywhere: CUPS asks the printer for its attributes, so no driver or PPD is needed
    try:
        await run_tool('lpadmin', '-p', queue, '-E', '-v', uri, '-m', 'everywhere', timeout=CUPS_TIMEOUT)
    except RuntimeError:
        # CUPS keeps the queue even when it could not make the PPD; a queue without one takes no jobs
        await run_tool('lpadmin', '-x', queue, timeout=CUPS_TIMEOUT, check=False)
        raise


async def ensure_queue(device: str, uri: str | None) -> tuple[str, str]:
    '''
    Makes sure the device has a CUPS queue that prints to its address. uri is the address found before, or
    None. If the printer doesn't answer there (its address may have changed), the address is looked up again
    once. Returns (queue name, the address used). Raises RuntimeError if the printer can't be reached.
    '''
    queue = queue_name(device)
    if uri is None:
        uri = await resolve_print_uri(device)
    try:
        if not await _queue_ready(queue, uri):
            await _add_queue(queue, uri)
    except RuntimeError:
        uri = await resolve_print_uri(device)   # the address may have changed: look once more
        await _add_queue(queue, uri)
    return queue, uri


def _offered(attributes: str, name: str, allowed: tuple[str, ...]) -> tuple[list[str], str]:
    '''
    The values of '<name>-supported' that the app knows how to ask for, in the app's order, and the
    '<name>-default' (the first offered value if the default isn't one of them).
    '''
    supported = (_ipp_value(attributes, f'{name}-supported') or '').split(',')
    offered = [value for value in allowed if value in supported]
    if not offered:
        raise RuntimeError(f"CUPS did not say which {name} values the printer has")
    default = _ipp_value(attributes, f'{name}-default')
    return offered, default if default in offered else offered[0]


async def printer_capabilities(queue: str) -> PrintCapabilities:
    '''The sides and colour modes this printer's queue offers, that this app knows how to ask for.'''
    attributes = await _queue_attributes(queue)
    sides, sides_default = _offered(attributes, 'sides', SIDES)
    colors, color_default = _offered(attributes, 'print-color-mode', COLOR_MODES)
    copies_max = COPIES_MAX
    if match := re.fullmatch(r'1-(\d+)', _ipp_value(attributes, 'copies-supported') or ''):
        copies_max = min(COPIES_MAX, int(match.group(1)))
    return PrintCapabilities(sides, sides_default, colors, color_default, copies_max)


######################### JOBS ######################

async def submit(queue: str, path: str, title: str, copies: int, sides: str, color_mode: str) -> str:
    '''
    Sends a PDF (or another file CUPS can print) to a queue, and returns the CUPS job id.
    Every value is checked against its allowlist before lp runs.
    '''
    if not re.fullmatch(QUEUE_PREFIX + r'[0-9a-f]{12}', queue):
        raise ValueError(f"'{queue}' is not a print queue of this app")
    if isinstance(copies, bool) or not isinstance(copies, int) or not 1 <= copies <= COPIES_MAX:
        raise ValueError(f"The number of copies must be from 1 to {COPIES_MAX}")
    if sides not in SIDES:
        raise ValueError(f"'{sides}' is not a way to print sides (use {'|'.join(SIDES)})")
    if color_mode not in COLOR_MODES:
        raise ValueError(f"'{color_mode}' is not a colour mode (use {'|'.join(COLOR_MODES)})")
    title = re.sub(r'[\x00-\x1f\x7f]', '', title)[:TITLE_LENGTH] or 'document'
    output = await run_tool('lp', '-d', queue, '-t', title, '-n', str(copies),
                            '-o', f'sides={sides}', '-o', f'print-color-mode={color_mode}',
                            '--', path, timeout=CUPS_TIMEOUT)
    match = re.search(r'request id is (\S+) \(', output)
    if match is None:
        raise RuntimeError(f"lp did not say which job it made: {output.strip()}")
    return match.group(1)


def _cups_printer_uri(queue: str) -> str:
    '''The IPP address of a queue on the CUPS server; a socket path in CUPS_SERVER is written as the host.'''
    server = os.environ.get('CUPS_SERVER', 'localhost')
    host = quote(server, safe='') if server.startswith('/') else server
    return f'ipp://{host}/printers/{queue}'


def _ipp_value(output: str, name: str) -> str | None:
    '''The value of one attribute in `ipptool -tv` output (the response's, not the echoed request's).'''
    match = re.search(rf'^\s+{re.escape(name)} \([^)]*\) = (.*)$', output, re.MULTILINE)
    return match.group(1).strip() if match else None


async def job_state(job_id: str) -> JobState:
    '''
    The state of a CUPS job, from its IPP job-state (lpstat's text can't tell a cancelled job from a
    printed one). A job CUPS no longer knows has left its history, and counts as completed.
    '''
    queue, _, number = job_id.rpartition('-')
    if not re.fullmatch(QUEUE_PREFIX + r'[0-9a-f]{12}', queue) or not number.isdigit():
        raise ValueError(f"'{job_id}' is not a print job of this app")
    output = await run_tool('ipptool', '-tv', '-d', f'job_id={number}', _cups_printer_uri(queue), JOB_STATE_TEST,
                            timeout=CUPS_TIMEOUT, check=False)
    if re.search(r'^\s+status-code = client-error-not-found', output, re.MULTILINE):
        return JobState(JOB_COMPLETED, '')
    state = _ipp_value(output, 'job-state')
    if state is None:
        raise RuntimeError(f"CUPS did not say how job {job_id} is doing:\n{output[-STDERR_KEPT:].strip()}")
    reasons = [reason for reason in (_ipp_value(output, 'job-state-reasons') or '').split(',')
               if reason not in ('', 'none', 'job-printing', 'job-completed-successfully')]
    reason = '; '.join(filter(None, [_ipp_value(output, 'job-printer-state-message')] + reasons))
    states = {'pending': JOB_PENDING, 'pending-held': JOB_PENDING, 'processing': JOB_PROCESSING,
              'processing-stopped': JOB_PROCESSING, 'completed': JOB_COMPLETED, 'canceled': JOB_CANCELED,
              'aborted': JOB_ABORTED}
    return JobState(states.get(state, JOB_PENDING), reason)


async def cancel(job_id: str):
    '''Removes a job from its queue, or stops it if it is printing.'''
    await run_tool('cancel', job_id, timeout=CUPS_TIMEOUT)
