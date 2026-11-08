import asyncio
from printer_backend.json_file import read_json, write_json
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
        return {entry['device']: Printer.from_dict(entry) for entry in read_json(self.path, [])}

    def _save(self):
        write_json(self.path, [printer.to_dict() for printer in self.printers.values()])

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

    async def find_new_printers(self) -> dict[str, str]:
        '''
        The devices on the network that aren't saved, as {device: the line scanimage -L printed for it}.
        Takes a while. Raises RuntimeError if the discovery fails.
        '''
        found = {}
        for line in await Printer.discover_printers():
            try:
                device = Printer.printer_name_from_discovery_line(line)
            except RuntimeError:    # not a device, e.g. 'No scanners were identified'
                continue
            if device not in self.printers:
                found[device] = line.strip()
        return found

    async def get_available_printers(self) -> list[Printer]:
        '''The saved printers that respond right now. Checked together, so a slow device doesn't delay the others.'''
        printers = self.get_all_printers()
        available = await asyncio.gather(*(printer.is_available() for printer in printers))
        return [printer for printer, is_available in zip(printers, available) if is_available]
