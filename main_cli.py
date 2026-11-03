from printer_backend.printer import Printer
from printer_backend.scanning import (
    FLIP, SCAN_FOLDER_DEFAULT, SCAN_FORMATS, Done, NeedsInput, PageScanned, Progress, source_kind)
import asyncio
from contextlib import aclosing

class CLI_APP():
    def __init__(self):
        self.current_printer = None

    async def select_printer(self):
        available_printers = await Printer.discover_printers()

        indexed_printers = [f'[{i}]: {name}' for i, name in enumerate(available_printers)]

        print(f"Select printer: \n\t{'\t'.join(indexed_printers)}")

        while selection := input(">> "):
            if not selection.isdigit():
                print("Input not numeric!")
                continue
            idx = int(selection)
            if not (0 <= idx < len(available_printers)): 
                print("Index out of range!")
                continue

            self.current_printer = Printer(Printer.printer_name_from_discovery_line(available_printers[idx]))
            await self.current_printer.load_options()
            break

    def display_printer_options(self):
        printer = self.current_printer
        print(f"Source: {printer.source} (change with 'source <name>': {' | '.join(printer.sources)})")
        for option in printer.current_options.values():
            if option.active:
                print('\t', option)

    def configure_printer(self):
        '''Changes options with '<option> <new_value>' until the user enters nothing.'''
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
        Returns the scan for the current configuration, or None if the user enters nothing. The source
        decides how it scans; the user only chooses if the source is not a known one.
        '''
        printer = self.current_printer
        kind = source_kind(printer.source)
        print(f"Scan from '{printer.source}' (this first empties '{SCAN_FOLDER_DEFAULT}'), Enter to quit: ")
        if kind is None:
            print("\t[0]: one page at a time\t[1]: everything in the source")
            choices = ('0', '1')
        else:
            print("\t[s]: start scanning")
            choices = ('s',)

        while selection := input(">> ").strip().lower():
            if selection not in choices:
                print("Invalid choice!")
                continue

            output_format = self.ask_output_format()
            is_pdf = output_format == 'pdf'     # only pdf pages can be combined
            # same defaults as the printer: separate pages one at a time, one document for a stack
            one_at_a_time = kind == 'flatbed' or selection == '0'
            combine = is_pdf and self.ask_yes_no("Combine the pages into one document?", not one_at_a_time)
            two_sided = kind == 'document-feeder' and self.ask_yes_no("Scan both sides?", False)
            return printer.scan(one_at_a_time=(selection == '0') if kind is None else None,
                                combine=combine, output_format=output_format, two_sided=two_sided)
        return None

    def handle_event(self, event):
        '''Shows an event of the scan, and returns the answer to send back if it asks for one.'''
        match event:
            case Progress(text):
                print(text, end='', flush=True)
            case PageScanned(path, number):
                print(f"\nPage {number} saved in '{path}'")
            case NeedsInput(kind) if kind == FLIP:
                input("Turn the stack around. Press Enter to continue ...")
            case NeedsInput():
                return input("Place the next page. Enter to scan it, 's' to stop >> ").strip().lower() != 's'
            case Done(files):
                print(f"\nDone: {', '.join(files)}")

    async def run_scan(self, scan):
        # aclosing: if we stop early (Ctrl-C), the running scanimage is stopped too
        async with aclosing(scan) as events:
            try:
                event = await anext(events)
                while True:
                    event = await events.asend(self.handle_event(event))
            except StopAsyncIteration:
                pass
            except (RuntimeError, ValueError) as error:
                print(f"\nScan failed: {error}")

    async def run(self):
        await self.select_printer()
        if self.current_printer is None:
            return

        while True:
            self.configure_printer()
            try:
                scan = self.choose_scan()
            except ValueError as error:
                print(error)
                continue
            if scan is None:
                break
            await self.run_scan(scan)


if __name__ == '__main__':
    app = CLI_APP()
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        print("\nStopped")