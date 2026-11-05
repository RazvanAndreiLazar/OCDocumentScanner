import asyncio
from asyncio import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
import re
import sys


DEFAULT_FILE = 'test'
PAGE_PREFIX = 'scan'
BACK_PREFIX = 'back'     # the back sides of a two-sided scan, until they get their page number
PAGE_FORMAT = 'png'

SCANIMG_VERSION = '1.2.1'
OPEN_ATTEMPTS = 4         # airscan sometimes can't open the device, the next try usually can
OPEN_RETRY_DELAY = 3      # seconds
OPEN_FAILED = 'open of device'    # how scanimage reports that, nothing was scanned at that point
AVAILABLE_TIMEOUT = 15    # seconds for a whole availability check, enough for all the retries
KILL_WAIT = 1             # seconds to wait for a killed process, see Printer._load_options_per_source
SCAN_FORMATS = ('tiff', 'pnm', 'png', 'jpeg', 'pdf')
SCAN_FOLDER_DEFAULT = './data/scans'
SCAN_KNOWN_KEYWORDS = {
    'flatbed': ['Flatbed'],                                     # can only scan one side at a time 
    'document-feeder': ['ADF', 'Automatic Document Feeder'],    # serves a stream of pages, only one sided
    'duplex': ['Duplex']                                        # serves a stream of pages, both sides
}

def parse_scanimage_version(text: str) -> str | None:
    '''"scanimage (sane-backends) 1.2.1-debian; backend version 1.2.1" -> "1.2.1"'''
    match = re.search(r'\(sane-backends\) (\d+(?:\.\d+)*)', text)
    return match.group(1) if match else None

async def init() -> str | None:
    '''
    Runs 'scanimage --version' to find if the version matches the implementation, printing a
    warning if it doesn't. Returns the version found, or None if it couldn't be read.
    '''
    try:
        proc = await subprocess.create_subprocess_exec('scanimage', '--version',
                                                       stdout=subprocess.PIPE,
                                                       stderr=subprocess.STDOUT)
    except FileNotFoundError:
        raise RuntimeError("'scanimage' was not found, is the sane-utils package installed?") from None
    stdout, _ = await proc.communicate()
    text = stdout.decode(errors='replace').strip()

    version = parse_scanimage_version(text)
    if version is None:
        print(f"Warning: could not read the scanimage version from '{text}'. "
              f"This was written for sane-backends {SCANIMG_VERSION}.", file=sys.stderr)
    elif version != SCANIMG_VERSION:
        print(f"Warning: scanimage is sane-backends {version}, but this was written for "
              f"{SCANIMG_VERSION}. Option parsing and scanning may not work as expected.", file=sys.stderr)
    return version


######################### EVENTS ######################
# What Printer.scan_all / Printer.scan_one_at_a_time yield. The caller shows them and, for
# NeedsInput, sends the answer back with asend().

@dataclass
class Progress():
    text: str       # what scanimage / img2pdf printed

@dataclass
class PageScanned():
    path: str
    number: int

NEXT_PAGE = 'next_page' # reply True to scan another page, False (or None) to stop
FLIP = 'flip'           # the front sides are done: turn the stack around, then reply with anything

@dataclass
class NeedsInput():
    kind: str           # NEXT_PAGE | FLIP

@dataclass
class Done():
    files: list[str]    # the documents (or pages) that were created


def source_kind(source: str) -> str | None:
    '''The SCAN_KNOWN_KEYWORDS kind a source name belongs to, or None if it is not known.'''
    name = source.lower()
    for kind in ('duplex', 'flatbed', 'document-feeder'):  # checked in this order: 'ADF Duplex' is a duplex
        if any(keyword.lower() in name for keyword in SCAN_KNOWN_KEYWORDS[kind]):
            return kind
    return None

async def run_command(*args) -> AsyncIterator[Progress]:
    '''
    Runs a command, yielding what it prints, and raises RuntimeError if it fails.
    Closing the generator stops the command.
    '''
    try:
        proc = await subprocess.create_subprocess_exec(*args,
                                                       stdout=subprocess.PIPE,
                                                       stderr=subprocess.STDOUT)  # one reader sees everything, in order
    except FileNotFoundError:
        raise RuntimeError(f"'{args[0]}' was not found, is it installed?") from None

    output = []
    try:
        # read(n) instead of readline(): -p progress is separated by '\r', not '\n'
        while chunk := await proc.stdout.read(1024):
            text = chunk.decode(errors='replace')
            output.append(text)
            yield Progress(text)
        if await proc.wait() != 0:
            raise RuntimeError(f"{args[0]} failed ({proc.returncode}): {''.join(output)}")
    finally:
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()
