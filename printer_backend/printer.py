import asyncio
from asyncio import subprocess
import re
from printer_backend.options import ScanOption, parse_help

class Printer():
    def __init__(self, device: str):
        self.device = device    # the name scanimage takes after -d
        self.options: dict[str, ScanOption] = {}

    async def load_options(self):
        '''Reads what the device supports from `scanimage -d DEVICE --help`.'''
        proc = await asyncio.create_subprocess_exec('scanimage', '-d', self.device, '--help',
                                                    stderr=subprocess.PIPE,
                                                    stdout=subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f'Reading options failed (Err Code: {proc.returncode})\n'
                               f'{stderr.decode(errors="replace")}')
        self.options = parse_help(stdout.decode(errors='replace'))

    def validate(self, settings: dict):
        for name, value in settings.items():
            option = self.options.get(name)
            if option is None:
                raise ValueError(f"'{self.device}' has no option '{name}'")
            if not option.active:
                raise ValueError(f"Option '{name}' is not available with the current settings")
            if not option.accepts(value):
                allowed = '|'.join(option.choices) if option.choices is not None \
                    else f'{option.min:g}..{option.max:g}'
                raise ValueError(f"'{value}' is not valid for '{name}' (allowed: {allowed}{option.unit})")

    def set_option(self, name: str, value):
        '''Changes the current value of an option, raising ValueError if the device doesn't accept it.'''
        self.validate({name: value})
        self.options[name].value = str(value)

    def to_args(self, settings: dict) -> list[str]:
        '''{'mode': 'Gray', 'resolution': 300} -> ['--mode', 'Gray', '--resolution', '300']'''
        self.validate(settings)
        args = []
        for name, value in settings.items():
            option = self.options[name]
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
