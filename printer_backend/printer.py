import asyncio
from asyncio import subprocess
import re
from printer_backend.options import ScanOption, parse_help

class Printer():
    def __init__(self, device: str):
        self.device = device    # the name scanimage takes after -d
        self.options: dict[str, ScanOption] = {}

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
