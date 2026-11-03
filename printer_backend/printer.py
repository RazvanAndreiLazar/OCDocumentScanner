import asyncio
from asyncio import subprocess
from collections.abc import AsyncGenerator
from contextlib import aclosing
import glob
import os
import re
import shutil
from printer_backend.options import ScanOption, parse_help
from printer_backend.scanning import (
    BACK_PREFIX, DEFAULT_FILE, FLIP, OPEN_ATTEMPTS, OPEN_RETRY_DELAY, NEXT_PAGE, PAGE_FORMAT, PAGE_PREFIX, SCAN_FOLDER_DEFAULT, SCAN_FORMATS,
    Done, NeedsInput, PageScanned, Progress, run_command, source_kind)

NO_SOURCE = ''    # key used for devices that have no 'source' option

# how a scan goes, depending on the kind of the source
ONE_AT_A_TIME = 'one at a time' # flatbed: a page per scanimage call
FEEDER = 'feeder'               # document feeder: a batch (twice if both sides, with a flip in between)
DUPLEX = 'duplex'               # feeder that scans both sides
UNKNOWN = 'unknown'             # not a known source: tried like a feeder

class Printer():
    def __init__(self, device: str):
        self.device = device    # the name scanimage takes after -d
        # what an option allows depends on the source (e.g. airscan offers fewer resolutions
        # from the feeder), so every source has its own options: {source: {name: ScanOption}}
        self.options: dict[str, dict[str, ScanOption]] = {}
        self.source = NO_SOURCE # the source whose options are shown and edited by default

    @property
    def sources(self) -> list[str]:
        return list(self.options)

    @property
    def current_options(self) -> dict[str, ScanOption]:
        return self.options_of(self.source)

    def select_source(self, source: str):
        self.options_of(source)     # raises ValueError if the device has no such source
        self.source = source

    def options_of(self, source: str) -> dict[str, ScanOption]:
        if source not in self.options:
            raise ValueError(f"'{self.device}' has no source '{source}'")
        return self.options[source]

    async def _load_options_per_source(self, source: str | None = None) -> dict[str, ScanOption]:
        args = [] if source is None else ['--source', source]
        # -A instead of --help: --help also lists every device on the network (including the
        # slow escl backend), which takes ~12 s; -A only talks to this one (~0.3 s)
        proc = await asyncio.create_subprocess_exec('scanimage', '-d', self.device, *args, '-A',
                                                    stderr=subprocess.PIPE,
                                                    stdout=subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f'Reading options failed (Err Code: {proc.returncode})\n'
                               f'{stderr.decode(errors="replace")}')
        return parse_help(stdout.decode(errors='replace'))

    async def load_options(self):
        '''Reads what the device supports, once for every source.'''
        default = await self._load_options_per_source()
        source = default.get('source')
        if source is None or source.choices is None:
            self.options = {NO_SOURCE: default}
            self.source = NO_SOURCE
            return

        # --source is passed even for the one that is already selected: hpaio lists
        # only up to 300 dpi until the source is given explicitly
        self.options = {choice: await self._load_options_per_source(choice) for choice in source.choices}
        self.source = source.value if source.value in self.options else source.choices[0]

    ######################### SCANNING ######################
    # scan picks scan_one_at_a_time or scan_all from the source. They don't talk to the user: they are
    # async generators that yield events (see scanning.py) and get the answers back with asend():
    #
    #   gen = printer.scan(two_sided=True)
    #   event = await anext(gen)
    #   while True:
    #       reply = None
    #       ...show the event; if it is a NeedsInput, ask and put the answer in reply...
    #       try: event = await gen.asend(reply)
    #       except StopAsyncIteration: break
    #
    # Closing the generator (await gen.aclose()) stops the running scanimage.

    @staticmethod
    async def clean_scans_folder():
        '''Empties the scans folder (creating it if needed), so that a new scan starts from nothing.'''
        os.makedirs(SCAN_FOLDER_DEFAULT, exist_ok=True)
        for entry in os.scandir(SCAN_FOLDER_DEFAULT):
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.remove(entry.path)

    @staticmethod
    def _check_scan_arguments(combine, output_format):
        if output_format not in SCAN_FORMATS:
            raise ValueError(f"Output format '{output_format}' not supported (must be {'|'.join(SCAN_FORMATS)})")
        if combine and output_format != 'pdf':
            raise ValueError("Only pdf formats can be combined")

    def _check_source_supports(self, one_at_a_time: bool):
        '''A flatbed holds one page at a time and a feeder takes the whole stack; unknown sources can do either.'''
        kind = source_kind(self.source)
        if kind is None:
            return
        if one_at_a_time and kind != 'flatbed':
            raise ValueError(f"Source '{self.source}' feeds the whole stack, it can't scan one page at a time")
        if not one_at_a_time and kind == 'flatbed':
            raise ValueError(f"Source '{self.source}' holds one page at a time, it can't scan a whole stack")

    def scan(self, one_at_a_time: bool | None = None, count: int | None = None, combine: bool | None = None,
             output_format='pdf', two_sided=False) -> AsyncGenerator:
        '''
        Scans in the way the current source works: scan_one_at_a_time for a flatbed, scan_all for a
        feeder or duplex. An unknown source has to say which one with one_at_a_time.
        count only applies to scanning one at a time and two_sided to a feeder; they are ignored otherwise.
        combine defaults to what the chosen method defaults to.
        '''
        if one_at_a_time is None:
            kind = source_kind(self.source)
            if kind is None:
                raise ValueError(f"Source '{self.source}' is not known, choose how to scan it with one_at_a_time")
            one_at_a_time = kind == 'flatbed'

        if one_at_a_time:
            return self.scan_one_at_a_time(count, False if combine is None else combine, output_format)
        return self.scan_all(True if combine is None else combine, output_format, two_sided)

    def scan_one_at_a_time(self, count: int | None = None, combine=False, output_format='pdf') -> AsyncGenerator:
        '''
        Scans one page at a time from the current source, yielding NeedsInput(NEXT_PAGE) before every
        page after the first one. Stops after 'count' pages, or when the reply is not True if count is None.
        Raises ValueError if the source is a feeder or duplex.
        '''
        self._check_source_supports(one_at_a_time=True)
        self._check_scan_arguments(combine, output_format)
        if count is not None and count < 1:
            raise ValueError("count must be at least 1, or None to scan until stopped")
        return self._scan(ONE_AT_A_TIME, count, combine, output_format)

    def scan_all(self, combine=True, output_format='pdf', two_sided=False) -> AsyncGenerator:
        '''
        Scans the whole stack: the document feeder (yielding NeedsInput(FLIP) between the sides if two_sided)
        or both sides with a duplex source (two_sided has no effect there). Raises ValueError if the source is a flatbed.
        '''
        self._check_source_supports(one_at_a_time=False)
        self._check_scan_arguments(combine, output_format)
        mode = {'document-feeder': FEEDER, 'duplex': DUPLEX}.get(source_kind(self.source), UNKNOWN)
        return self._scan(mode, None, combine, output_format, two_sided)

    def _pages(self, page_format: str, prefix=PAGE_PREFIX) -> list[str]:
        # zero-padded numbers: the sorted names are in page order
        return sorted(glob.glob(os.path.join(SCAN_FOLDER_DEFAULT, f'{prefix}*.{page_format}')))

    async def _scanimage(self, *args) -> AsyncGenerator:
        for attempt in range(1, OPEN_ATTEMPTS + 1):
            command = run_command('scanimage', '-d', self.device, *self.to_args(), *args)
            try:
                async with aclosing(command) as events:
                    async for event in events:
                        yield event
                return
            except RuntimeError as error:
                # nothing was scanned if the device didn't open, so it is safe to try again
                if 'open of device' not in str(error) or attempt == OPEN_ATTEMPTS:
                    raise
            yield Progress(f"Could not open the device, trying again in {OPEN_RETRY_DELAY} s ({attempt}/{OPEN_ATTEMPTS - 1})\n")
            await asyncio.sleep(OPEN_RETRY_DELAY)

    async def _scan(self, mode, count, combine, output_format, two_sided=False) -> AsyncGenerator:
        await self.clean_scans_folder()
        # scanimage can't write one pdf per page of a batch: the pages are scanned as images and
        # turned into pdf afterwards
        page_format = PAGE_FORMAT if output_format == 'pdf' else output_format
        batch = os.path.join(SCAN_FOLDER_DEFAULT, f'{PAGE_PREFIX}%03d.{page_format}')

        if mode == ONE_AT_A_TIME:
            number = 0
            while count is None or number < count:
                if number and not (yield NeedsInput(NEXT_PAGE)):
                    break
                number += 1
                path = os.path.join(SCAN_FOLDER_DEFAULT, f'{PAGE_PREFIX}{number:03d}.{page_format}')
                async with aclosing(self._scanimage('--format', page_format, '-p', '-o', path)) as events:
                    async for event in events:
                        yield event
                yield PageScanned(path, number)

        elif mode == DUPLEX and output_format == 'pdf' and combine:
            # the scanner serves both sides: scanimage writes the whole document itself
            document = os.path.join(SCAN_FOLDER_DEFAULT, f'{DEFAULT_FILE}.pdf')
            async with aclosing(self._scanimage('--format', 'pdf', '-p', f'--batch={document}')) as events:
                async for event in events:
                    yield event
            yield Done([document])
            return

        else:
            if mode == UNKNOWN:
                yield Progress(f"Warning: don't know the source '{self.source}', scanning it like a document feeder\n")
            # a feeder: the fronts first (odd page numbers if the backs follow), then the backs
            two_sided = two_sided and mode == FEEDER
            fronts = [f'--batch={batch}', '--batch-start=1', '--batch-increment=2'] if two_sided else [f'--batch={batch}']
            async with aclosing(self._scanimage('--format', page_format, '-p', *fronts)) as events:
                async for event in events:
                    yield event
            if two_sided:
                yield NeedsInput(FLIP)
                # numbered upwards and renamed afterwards: with a negative --batch-increment, scanimage
                # exits with an error when the feeder runs out, however many pages it scanned
                backs = os.path.join(SCAN_FOLDER_DEFAULT, f'{BACK_PREFIX}%03d.{page_format}')
                async with aclosing(self._scanimage('--format', page_format, '-p', f'--batch={backs}')) as events:
                    async for event in events:
                        yield event
                sheets = len(self._pages(page_format))
                scanned_backs = self._pages(page_format, BACK_PREFIX)
                if len(scanned_backs) != sheets:
                    raise RuntimeError(f"Scanned {sheets} front sides but {len(scanned_backs)} back sides, "
                                       f"the pages are in '{SCAN_FOLDER_DEFAULT}'")
                # the flipped stack feeds the last sheet's back first: the first back is the last page
                for index, path in enumerate(scanned_backs):
                    os.rename(path, batch % (2 * (sheets - index)))
            for number, path in enumerate(self._pages(page_format), 1):
                yield PageScanned(path, number)

        pages = self._pages(page_format)
        if not pages:
            raise RuntimeError("Nothing was scanned")
        if output_format != 'pdf':
            files = pages
        elif combine:
            files = [os.path.join(SCAN_FOLDER_DEFAULT, f'{DEFAULT_FILE}.pdf')]
            async with aclosing(run_command('img2pdf', *pages, '-o', files[0])) as events:
                async for event in events:
                    yield event
        else:
            files = [os.path.splitext(page)[0] + '.pdf' for page in pages]
            for page, document in zip(pages, files):
                async with aclosing(run_command('img2pdf', page, '-o', document)) as events:
                    async for event in events:
                        yield event
        yield Done(files)

    def validate(self, settings: dict):
        options = self.current_options
        for name, value in settings.items():
            if name == 'source':
                raise ValueError("The source is a separate argument, not a setting")
            option = options.get(name)
            if option is None:
                raise ValueError(f"'{self.device}' has no option '{name}' for source '{self.source}'")
            if not option.active:
                raise ValueError(f"Option '{name}' is not available with the current settings")
            if not option.accepts(value):
                allowed = '|'.join(option.choices) if option.choices is not None \
                    else f'{option.min:g}..{option.max:g}'
                raise ValueError(f"'{value}' is not valid for '{name}' (allowed: {allowed}{option.unit})")

    def set_option(self, name: str, value):
        '''Changes the value of an option of the current source, raising ValueError if the device doesn't accept it.'''
        self.validate({name: value})
        self.current_options[name].value = str(value)

    def to_args(self) -> list[str]:
        '''
        The scanimage arguments for the current source and the options the user changed there, e.g.
        ['--source', 'ADF', '--mode', 'Gray', '--resolution', '300']. The options that still have the
        value the device listed are not sent: sending them all makes hpaio refuse -y, because it
        lists 297.011 but doesn't accept it back.
        '''
        options = self.current_options
        changed = {name: option.value for name, option in options.items()
                   if name != 'source' and option.active and option.value is not None
                   and option.value != option.default}
        self.validate(changed)
        args = [options['source'].flag, self.source] if 'source' in options else []
        for name, value in changed.items():
            option = options[name]
            if option.is_boolean:
                args.append(f'{option.flag}={value}')
            else:
                args += [option.flag, str(value)]
        return args

    '''
    Printer discovery
    '''
    @staticmethod
    async def discover_printers():
        proc = await asyncio.create_subprocess_exec('scanimage', '-L', 
                                                    stderr=subprocess.PIPE, 
                                                    stdout=subprocess.PIPE)


        output = []
        while line := await proc.stdout.readline():
            text = line.decode(errors='replace')
            # skip the gzip noise printed by the broken escl backend
            if '�' in text or not text.rstrip().isprintable():
                continue
            output.append(text)

        if await proc.wait() != 0:
            raise RuntimeError(f'Printer discovery failed (Err Code: {proc.returncode}\nOutput: {''.join(output)}')
        return output

    @staticmethod
    def printer_name_from_discovery_line(line: str):
        # scanimage -L line: device `NAME' is a VENDOR MODEL TYPE
        match = re.match(r"device `(.*)' is a ", line)
        if not match:
            raise RuntimeError("Could not read the device name from that entry!")

        return match.group(1)
