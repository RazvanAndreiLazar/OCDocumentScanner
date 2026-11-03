from printer_backend.printer import Printer
from printer_backend.printer_repository import PrinterRepository
from printer_backend.settings_repository import SettingsRepository
from printer_backend.storage import DEFAULT_SCAN_NAME, LocalFolderStorage, check_scan_name
from printer_backend.scanning import (
    FLIP, SCAN_FOLDER_DEFAULT, SCAN_FORMATS, Done, NeedsInput, PageScanned, Progress, source_kind, init)
import asyncio
from contextlib import aclosing

class CLI_APP():
    def __init__(self, repository: PrinterRepository | None = None, settings: SettingsRepository | None = None):
        self.repository = PrinterRepository() if repository is None else repository
        self.settings = SettingsRepository() if settings is None else settings
        self.current_printer = None

    def ask_index(self, title, entries):
        '''Returns the position of the entry the user picks, or None if the user enters nothing.'''
        print(title)
        for index, entry in enumerate(entries):
            print(f"\t[{index}]: {entry}")

        while selection := input(">> ").strip():
            if not selection.isdecimal():
                print("Input not numeric!")
                continue
            index = int(selection)
            if not (0 <= index < len(entries)):
                print("Index out of range!")
                continue
            return index
        return None

    async def select_printer(self):
        '''Makes a saved printer the current one, or leaves it None if the user enters nothing.'''
        if not self.repository.get_all_printers():
            print("No saved printers yet.")
            await self.add_printer()
        printers = self.repository.get_all_printers()
        if not printers:
            return

        index = self.ask_index("Select printer (Enter to quit):", [printer.device for printer in printers])
        if index is None:
            return

        printer = printers[index]
        print("Checking the printer ...")
        if not await printer.is_available():
            print(f"Warning: '{printer.device}' is not available right now, scanning may fail")
        self.current_printer = printer

    async def add_printer(self):
        '''Saves one of the discovered printers that isn't saved yet.'''
        print("Looking for printers, this can take a while ...")
        try:
            lines = await Printer.discover_printers()
        except RuntimeError as error:
            print(error)
            return

        saved = {printer.device for printer in self.repository.get_all_printers()}
        found = {}
        for line in lines:
            try:
                device = Printer.printer_name_from_discovery_line(line)
            except RuntimeError:    # not a device, e.g. 'No scanners were identified'
                continue
            if device not in saved:
                found[device] = line.strip()
        if not found:
            print("No new printers found")
            return

        index = self.ask_index("Add printer (Enter to go back):", list(found.values()))
        if index is None:
            return

        printer = Printer(list(found)[index])
        try:
            await printer.load_options()
        except RuntimeError as error:
            print(error)
            return
        self.repository.add_printer(printer)
        print(f"Added '{printer.device}'")

    def delete_printer(self):
        device = self.current_printer.device
        if self.ask_yes_no(f"Delete '{device}' and its saved options?", False):
            self.repository.remove_printer(device)
            self.current_printer = None

    def display_printer_options(self):
        printer = self.current_printer
        print(f"Source: {printer.source} (change with 'source <name>': {' | '.join(printer.sources)})")
        for option in printer.current_options.values():
            if option.active:
                print('\t', option)

    def configure_printer(self):
        '''Changes and saves options with '<option> <new_value>' until the user enters nothing.'''
        while True:
            self.display_printer_options()
            entry = input("<option> <new_value> (Enter to finish) >> ").strip()
            if not entry:
                break

            # values can hold spaces (e.g. 'Text and Graphic'), option names can't
            name, _, value = entry.partition(' ')
            value = value.strip()
            if not value:
                print("Expected: <option> <new_value>")
                continue

            try:
                if name == 'source':
                    self.current_printer.select_source(value)
                else:
                    self.current_printer.set_option(name, value)
                self.repository.update_printer(self.current_printer)
            except ValueError as error:
                print(error)

    def ask_yes_no(self, question, default):
        while True:
            answer = input(f"{question} [{'Y/n' if default else 'y/N'}] >> ").strip().lower()
            if not answer:
                return default
            if answer in ('y', 'yes'):
                return True
            if answer in ('n', 'no'):
                return False
            print("Expected y or n!")

    def ask_output_format(self):
        while True:
            answer = input(f"Output format ({'|'.join(SCAN_FORMATS)}, Enter for pdf) >> ").strip().lower() or 'pdf'
            if answer in SCAN_FORMATS:
                return answer
            print("Format not supported!")

    def choose_scan(self):
        '''
        Returns the scan for the current configuration. The source decides how it scans; the user only
        chooses if the source is not a known one, and gets None back by entering nothing there.
        '''
        printer = self.current_printer
        kind = source_kind(printer.source)
        print(f"Scan from '{printer.source}', saving in '{self.settings.save_location}'")
        selection = None
        if kind is None:
            print("\t[0]: one page at a time\t[1]: everything in the source")
            while (selection := input("(Enter to go back) >> ").strip().lower()) not in ('0', '1'):
                if not selection:
                    return None
                print("Invalid choice!")

        output_format = self.ask_output_format()
        is_pdf = output_format == 'pdf'     # only pdf pages can be combined
        # same defaults as the printer: separate pages one at a time, one document for a stack
        one_at_a_time = kind == 'flatbed' or selection == '0'
        combine = is_pdf and self.ask_yes_no("Combine the pages into one document?", not one_at_a_time)
        two_sided = kind == 'document-feeder' and self.ask_yes_no("Scan both sides?", False)
        return printer.scan(one_at_a_time=(selection == '0') if kind is None else None,
                            combine=combine, output_format=output_format, two_sided=two_sided)

    def ask_scan_name(self):
        while True:
            name = input(f"Name (Enter for {DEFAULT_SCAN_NAME}) >> ").strip() or DEFAULT_SCAN_NAME
            try:
                check_scan_name(name)
                return name
            except ValueError as error:
                print(error)

    def handle_event(self, event):
        '''Shows an event of the scan, and returns the answer to send back if it asks for one.'''
        match event:
            case Progress(text):
                print(text, end='', flush=True)
            case PageScanned(_, number):
                print(f"\nPage {number} scanned")
            case NeedsInput(kind) if kind == FLIP:
                input("Turn the stack around. Press Enter to continue ...")
            case NeedsInput():
                return input("Place the next page. Enter to scan it, 's' to stop >> ").strip().lower() != 's'

    async def run_scan(self, scan, name):
        '''Runs the scan and saves the finished files under this name.'''
        files = None
        # aclosing: if we stop early (Ctrl-C), the running scanimage is stopped too
        async with aclosing(scan) as events:
            try:
                event = await anext(events)
                while True:
                    if isinstance(event, Done):
                        files = event.files
                    event = await events.asend(self.handle_event(event))
            except StopAsyncIteration:
                pass
            except (RuntimeError, ValueError) as error:
                print(f"\nScan failed: {error}")
                return
        if files:
            await self.save_scan(files, name)

    async def save_scan(self, files, name):
        location = self.settings.save_location
        try:
            saved = await location.save(files, name)
        except RuntimeError as error:
            print(f"\nSaving failed: {error}\nThe scan stays in '{SCAN_FOLDER_DEFAULT}' until the next scan")
            return
        print(f"\nSaved in '{location}': {', '.join(saved)}")
        await Printer.clean_scans_folder()

    async def scan(self):
        try:
            await self.settings.save_location.validate_storage_path()   # before any paper moves
            scan = self.choose_scan()
        except (RuntimeError, ValueError) as error:
            print(error)
            return
        if scan is not None:
            await self.run_scan(scan, self.ask_scan_name())

    async def change_save_location(self):
        print(f"Scans are saved in '{self.settings.save_location}'")
        path = input("New folder (Enter to keep it) >> ").strip()
        if not path:
            return
        location = LocalFolderStorage(path)
        try:
            await location.validate_storage_path()
        except RuntimeError as error:
            print(error)
            return
        self.settings.save_location = location
        print(f"Scans will be saved in '{location}'")

    async def printer_menu(self):
        '''Asks what to do with the current printer and does it. Returns False if the user wants to quit.'''
        printer = self.current_printer
        print(f"\nPrinter: {printer.device}, source: {printer.source}")
        print("\t[1]: add new printer\n\t[2]: change printer\n\t[3]: configure scanner\n\t[4]: scan"
              f"\n\t[5]: change save location ({self.settings.save_location})\n\t[d]: delete this printer")
        match input("(Enter to quit) >> ").strip().lower():
            case '':
                return False
            case '1':
                await self.add_printer()
            case '2':
                self.current_printer = None
            case '3':
                self.configure_printer()
            case '4':
                await self.scan()
            case '5':
                await self.change_save_location()
            case 'd':
                self.delete_printer()
            case _:
                print("Invalid choice!")
        return True

    async def run(self):
        await init()
        while True:
            if self.current_printer is None:
                await self.select_printer()
                if self.current_printer is None:
                    return
            if not await self.printer_menu():
                return


if __name__ == '__main__':
    app = CLI_APP()
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        print("\nStopped")