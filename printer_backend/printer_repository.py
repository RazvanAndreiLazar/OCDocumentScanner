import asyncio
import json
import os
from printer_backend.printer import Printer

PRINTERS_FILE_DEFAULT = './data/printers.json'

class PrinterRepository():
    '''
    The saved printers, kept in a json file that is rewritten on every change. Only
    get_available_printers talks to the devices: settings can be read and changed while a
    scanner is off or busy, callers that need the device use Printer.is_available.
    '''
    def __init__(self, path: str = PRINTERS_FILE_DEFAULT):
        self.path = path
        self.printers: dict[str, Printer] = self._load()

    def _load(self) -> dict[str, Printer]:
        try:
            with open(self.path, encoding='utf-8') as file:
                data = json.load(file)
        except FileNotFoundError:
            return {}
        return {entry['device']: Printer.from_dict(entry) for entry in data}

    def _save(self):
        # written next to the file and moved over it: a crash can't leave half of a file behind
        temporary = f'{self.path}.tmp'
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        with open(temporary, 'w', encoding='utf-8') as file:
            json.dump([printer.to_dict() for printer in self.printers.values()], file, indent=2)
        os.replace(temporary, self.path)

    def add_printer(self, printer: Printer):
        '''Saves a printer whose options were loaded, raising ValueError if it is already saved.'''
        if printer.device in self.printers:
            raise ValueError(f"'{printer.device}' is already saved")
        self.printers[printer.device] = printer
        self._save()

    def update_printer(self, printer: Printer):
        '''Replaces the saved printer with the same device, raising ValueError if there is none.'''
        self.get_printer(printer.device)
        self.printers[printer.device] = printer
        self._save()

    def update_options(self, device: str, settings: dict, source: str | None = None) -> Printer:
        '''
        Changes one or more options of a source (the printer's current one by default) and saves them.
        Raises ValueError and changes nothing if the printer isn't saved or an option is not accepted.
        '''
        printer = self.get_printer(device)
        printer.set_options(settings, source)
        self._save()
        return printer

    def remove_printer(self, device: str):
        '''Raises ValueError if the printer isn't saved.'''
        self.get_printer(device)
        del self.printers[device]
        self._save()

    def get_printer(self, device: str) -> Printer:
        '''Raises ValueError if the printer isn't saved.'''
        if device not in self.printers:
            raise ValueError(f"'{device}' is not saved")
        return self.printers[device]

    def get_all_printers(self) -> list[Printer]:
        return list(self.printers.values())

    async def get_available_printers(self) -> list[Printer]:
        '''The saved printers that respond right now. Checked together, so a slow device doesn't delay the others.'''
        printers = self.get_all_printers()
        available = await asyncio.gather(*(printer.is_available() for printer in printers))
        return [printer for printer, is_available in zip(printers, available) if is_available]
