import asyncio
from asyncio import subprocess
import re
from printer_backend.options import ScanOption, parse_help

NO_SOURCE = ''    # key used for devices that have no 'source' option

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

    def to_args(self, settings: dict) -> list[str]:
        '''With source 'ADF': {'mode': 'Gray', 'resolution': 300} -> ['--source', 'ADF', '--mode', 'Gray', '--resolution', '300']'''
        self.validate(settings)
        options = self.current_options
        args = [options['source'].flag, self.source] if 'source' in options else []
        for name, value in settings.items():
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
