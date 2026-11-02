from printer_backend.printer import Printer
import asyncio

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
        for option in self.current_printer.options.values():
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
                self.current_printer.set_option(name, value)
            except ValueError as error:
                print(error)

    async def run(self):
        await self.select_printer()
        self.configure_printer()
        print(self.current_printer.device)


if __name__ == '__main__':
    app = CLI_APP()
    asyncio.run(app.run())