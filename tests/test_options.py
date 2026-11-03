import unittest
from pathlib import Path

from printer_backend.options import parse_help
from printer_backend.printer import NO_SOURCE, Printer

FIXTURES = Path(__file__).parent / 'fixtures'
SOURCE = 'Flatbed'     # a source every fixture offers


def printer_with(options):
    '''A Printer whose sources all offer these options.'''
    printer = Printer('dev')
    printer.options = {choice: options for choice in options['source'].choices}
    printer.select_source(SOURCE)
    return printer


class OptionTypesMixin():
    '''
    Checks every option of one device, grouped by option type. Subclasses give the
    fixture and the expected tables; test_every_option_is_covered fails if the
    fixture has an option that no table lists.
    '''
    fixture: Path
    # name: (flag, choices, unit, value)
    enumerations: dict
    # name: (flag, min, max, unit, value)
    ranges: dict
    # name: (flag, value)
    booleans: dict
    # names of the options marked [inactive]
    inactive: set
    # options that carry the [advanced] tag
    advanced: set

    def setUp(self):
        self.options = parse_help(self.fixture.read_text())
        self.printer = printer_with(self.options)

    def test_every_option_is_covered(self):
        expected = set(self.enumerations) | set(self.ranges) | set(self.booleans)
        self.assertEqual(set(self.options), expected)

    def test_enumerations(self):
        for name, (flag, choices, unit, value) in self.enumerations.items():
            with self.subTest(name):
                option = self.options[name]
                self.assertEqual((option.flag, option.choices, option.unit, option.value),
                                 (flag, choices, unit, value))
                self.assertFalse(option.is_boolean)
                for choice in choices:
                    self.assertTrue(option.accepts(choice))
                self.assertFalse(option.accepts('not-a-choice'))

    def test_ranges(self):
        for name, (flag, low, high, unit, value) in self.ranges.items():
            with self.subTest(name):
                option = self.options[name]
                self.assertEqual((option.flag, option.min, option.max, option.unit, option.value),
                                 (flag, low, high, unit, value))
                self.assertIsNone(option.choices)
                self.assertTrue(option.accepts(low))
                self.assertTrue(option.accepts(high))
                self.assertFalse(option.accepts(low - 1))
                self.assertFalse(option.accepts(high + 1))
                self.assertFalse(option.accepts('abc'))

    def test_booleans(self):
        for name, (flag, value) in self.booleans.items():
            with self.subTest(name):
                option = self.options[name]
                self.assertEqual((option.flag, option.choices, option.value),
                                 (flag, ['yes', 'no'], value))
                self.assertTrue(option.is_boolean)
                # an optional argument can only be attached with '='
                self.printer.set_option(name, 'yes')
                self.assertEqual(self.printer.to_args(), ['--source', SOURCE, f'{flag}=yes'])
                self.assertFalse(option.accepts('maybe'))

    def test_inactive(self):
        for name, option in self.options.items():
            with self.subTest(name):
                self.assertEqual(option.active, name not in self.inactive)
                if name in self.inactive:
                    # an inactive option has no current value to show
                    self.assertIsNone(option.value)
                    with self.assertRaises(ValueError):
                        self.printer.validate({name: option.min if option.choices is None else option.choices[0]})

    def test_advanced_tag_is_not_the_value(self):
        for name in self.advanced:
            with self.subTest(name):
                self.assertNotEqual(self.options[name].value, 'advanced')
                self.assertIsNotNone(self.options[name].value)

    def test_str_of_every_option(self):
        for name, option in self.options.items():
            with self.subTest(name):
                self.assertTrue(str(option).startswith(name))

    def test_to_args_for_every_option(self):
        for name, option in self.options.items():
            if not option.active or name == 'source':
                continue
            with self.subTest(name):
                # a value other than the one the device listed, which is not sent
                candidates = option.choices if option.choices is not None else [option.min, option.max]
                value = next((v for v in candidates if str(v) != option.default and option.accepts(v)), None)
                if value is None:
                    self.assertEqual(self.printer.to_args(), ['--source', SOURCE])
                    continue
                self.printer.set_option(name, value)
                args = self.printer.to_args()
                if option.is_boolean:
                    self.assertEqual(args, ['--source', SOURCE, f'{option.flag}={value}'])
                else:
                    self.assertEqual(args, ['--source', SOURCE, option.flag, str(value)])
                self.printer.set_option(name, option.default)

    def test_source_is_not_a_setting(self):
        for source in self.options['source'].choices:
            with self.subTest(source), self.assertRaises(ValueError):
                self.printer.set_option('source', source)


class HpaioOptionsTest(OptionTypesMixin, unittest.TestCase):
    '''The hpaio backend, scanning from the flatbed.'''
    fixture = FIXTURES / 'envy_6400_hpaio_flatbed.txt'
    enumerations = {
        'mode': ('--mode', ['Lineart', 'Gray', 'Color'], '', 'Color'),
        'resolution': ('--resolution', ['75', '100', '150', '200', '300', '400', '600', '1200'], 'dpi', '75'),
        'source': ('--source', ['Flatbed', 'ADF'], '', 'Flatbed'),
        'compression': ('--compression', ['JPEG'], '', 'JPEG'),
    }
    ranges = {
        'jpeg-quality': ('--jpeg-quality', 0, 100, '', None),
        'l': ('-l', 0, 215.9, 'mm', '0'),
        't': ('-t', 0, 297.011, 'mm', '0'),
        'x': ('-x', 0, 215.9, 'mm', '215.9'),
        'y': ('-y', 0, 297.011, 'mm', '297.011'),
        'multi-pick': ('--multi-pick', 0, 1, '', '0'),
    }
    booleans = {}
    inactive = {'jpeg-quality'}
    advanced = {'compression', 'multi-pick'}

    def test_ignores_garbage_after_device_options(self):
        noisy = self.fixture.read_text() + "\nList of available devices:\x1f\x8b\x08\n    --fake A|B [A]\n"
        self.assertEqual(set(parse_help(noisy)), set(self.options))


class AirscanOptionsTest(OptionTypesMixin, unittest.TestCase):
    '''Full `--help` output of the same printer through the airscan backend.'''
    fixture = FIXTURES / 'envy_6400_help.txt'
    enumerations = {
        'resolution': ('--resolution', ['75', '100', '150', '200', '300', '400', '600', '1200'], 'dpi', '300'),
        'mode': ('--mode', ['Color', 'Gray'], '', 'Color'),
        # values with spaces and asterisks
        'scan-intent': ('--scan-intent',
                        ['*unset*', 'Document', 'Text and Graphic', 'Photo', 'Preview'], '', '*unset*'),
        'source': ('--source', ['Flatbed', 'ADF'], '', 'Flatbed'),
    }
    ranges = {
        'l': ('-l', 0, 215.9, 'mm', '0'),
        't': ('-t', 0, 297.011, 'mm', '0'),
        'x': ('-x', 0, 215.9, 'mm', '215.9'),
        'y': ('-y', 0, 297.011, 'mm', '297.011'),
        # '-100..100% (in steps of 1)'
        'brightness': ('--brightness', -100, 100, '%', '0'),
        'contrast': ('--contrast', -100, 100, '%', '0'),
        'shadow': ('--shadow', 0, 100, '%', '0'),
        'highlight': ('--highlight', 0, 100, '%', '100'),
        'analog-gamma': ('--analog-gamma', 0.0999908, 4, '', '1'),
    }
    booleans = {
        'negative': ('--negative', 'no'),
    }
    inactive = set()
    advanced = set()

    def test_ignores_generic_scanimage_options_and_device_list(self):
        for generic in ('batch-start', 'format', 'progress'):
            self.assertNotIn(generic, self.options)
        self.assertEqual(list(self.options)[0], 'resolution')
        self.assertEqual(list(self.options)[-1], 'negative')

    def test_validation(self):
        self.printer.validate({'mode': 'Gray', 'resolution': 1200, 'brightness': -50})
        for settings in ({'mode': 'Lineart'}, {'brightness': 101}, {'negative': 'maybe'}, {'analog-gamma': 0}):
            with self.assertRaises(ValueError, msg=settings):
                self.printer.validate(settings)


class PrinterSettingsTest(unittest.TestCase):
    def setUp(self):
        # the feeder listing: resolutions stop at 300 dpi
        self.printer = printer_with(parse_help((FIXTURES / 'envy_6400_hpaio_adf.txt').read_text()))
        self.options = self.printer.options_of(SOURCE)

    def test_to_args(self):
        self.printer.set_option('mode', 'Gray')
        self.printer.set_option('resolution', 300)
        self.assertEqual(self.printer.to_args(),
                         ['--source', SOURCE, '--mode', 'Gray', '--resolution', '300'])

    def test_rejects_bad_values(self):
        for settings in ({'resolution': '600'}, {'mode': 'Foo'}, {'x': 999}, {'nope': 1}, {'jpeg-quality': 50}):
            with self.assertRaises(ValueError, msg=settings):
                self.printer.validate(settings)

    def test_set_option_changes_the_value(self):
        self.printer.set_option('mode', 'Gray')
        self.printer.set_option('resolution', 300)
        self.assertEqual(self.options['mode'].value, 'Gray')
        self.assertEqual(self.options['resolution'].value, '300')

    def test_set_option_keeps_the_value_when_rejected(self):
        for name, value in (('mode', 'Foo'), ('resolution', '600'), ('nope', '1'), ('jpeg-quality', '50'),
                            ('source', 'ADF')):
            with self.subTest(name), self.assertRaises(ValueError):
                self.printer.set_option(name, value)
        self.assertEqual(self.options['mode'].value, 'Color')
        self.assertEqual(self.options['resolution'].value, '75')
        self.assertIsNone(self.options['jpeg-quality'].value)

    def test_unknown_source(self):
        with self.assertRaises(ValueError):
            self.printer.options_of('Duplex')


class AllOptionsOutputTest(unittest.TestCase):
    '''`scanimage -d DEVICE -A` is what the printer loads: it is much faster than --help.'''
    def setUp(self):
        self.options = parse_help((FIXTURES / 'envy_6400_airscan_all_options.txt').read_text())

    def test_same_options_as_help_plus_the_inactive_ones(self):
        from_help = parse_help((FIXTURES / 'envy_6400_airscan_flatbed.txt').read_text())
        extra = set(self.options) - set(from_help)
        self.assertEqual(extra, {'adf-justification-x', 'adf-justification-y'})
        for name, option in from_help.items():
            self.assertEqual(self.options[name], option, name)

    def test_free_text_options(self):
        for name in ('adf-justification-x', 'adf-justification-y'):
            with self.subTest(name):
                option = self.options[name]
                self.assertEqual((option.choices, option.min, option.value), (None, None, None))
                self.assertFalse(option.active)
                self.assertEqual(str(option), f'{name} (inactive)')

    def test_active_free_text_option(self):
        text = "Options specific to device `x':\n    --label <string> [front]\n    --count <int> [3]\n"
        options = parse_help(text)
        for name, value in (('label', 'front'), ('count', '3')):
            with self.subTest(name):
                option = options[name]
                self.assertEqual((option.choices, option.min, option.value), (None, None, value))
                self.assertTrue(option.accepts('anything'))
                self.assertEqual(str(option), f'{name} ({value})')


# `--help` output of the printer for every source, as scanimage gives it with `--source X`;
# None is the output without --source
HELP_FILES = {
    'hpaio': {None: 'envy_6400_hpaio_flatbed.txt',
              'Flatbed': 'envy_6400_hpaio_flatbed.txt', 'ADF': 'envy_6400_hpaio_adf.txt'},
    'airscan': {None: 'envy_6400_help.txt',
                'Flatbed': 'envy_6400_airscan_flatbed.txt', 'ADF': 'envy_6400_airscan_adf.txt'},
}


class LoadOptionsTest(unittest.IsolatedAsyncioTestCase):
    '''Options are kept per source, because a source can change what the other options allow.'''
    async def load(self, backend):
        printer = Printer(backend)
        self.calls = []

        async def read_help(*args):
            self.calls.append(args)
            source = args[0] if args else None
            return parse_help((FIXTURES / HELP_FILES[backend][source]).read_text())

        printer._load_options_per_source = read_help
        await printer.load_options()
        return printer

    async def test_loads_the_options_of_every_source(self):
        for backend in HELP_FILES:
            with self.subTest(backend):
                printer = await self.load(backend)
                self.assertEqual(printer.sources, ['Flatbed', 'ADF'])
                self.assertIs(printer.options_of('Flatbed'), printer.options['Flatbed'])
                self.assertEqual(printer.options_of('ADF')['source'].value, 'ADF')

    async def test_current_source_starts_on_the_device_default(self):
        for backend in HELP_FILES:
            with self.subTest(backend):
                printer = await self.load(backend)
                self.assertEqual(printer.source, 'Flatbed')
                self.assertIs(printer.current_options, printer.options['Flatbed'])

    async def test_select_source(self):
        printer = await self.load('airscan')
        printer.select_source('ADF')
        self.assertEqual(printer.source, 'ADF')
        self.assertIs(printer.current_options, printer.options['ADF'])
        # the options of the other source are untouched
        self.assertEqual(printer.options['Flatbed']['source'].value, 'Flatbed')

    async def test_rejected_source_keeps_the_current_one(self):
        printer = await self.load('hpaio')
        with self.assertRaises(ValueError):
            printer.select_source('Duplex')
        self.assertEqual(printer.source, 'Flatbed')

    async def test_source_is_passed_even_when_it_is_the_default(self):
        printer = await self.load('hpaio')
        self.assertIn(('Flatbed',), self.calls)
        self.assertIn(('ADF',), self.calls)

    async def test_resolutions_depend_on_the_source(self):
        for backend in HELP_FILES:
            with self.subTest(backend):
                printer = await self.load(backend)
                self.assertEqual(printer.options['Flatbed']['resolution'].choices[-3:], ['400', '600', '1200'])
                self.assertEqual(printer.options['ADF']['resolution'].choices[-1], '300')

    async def test_device_without_source_option(self):
        printer = Printer('dev')
        listing = {name: option for name, option in parse_help((FIXTURES / HELP_FILES['hpaio'][None]).read_text()).items()
                   if name != 'source'}

        async def read_help(*args):
            return listing

        printer._load_options_per_source = read_help
        await printer.load_options()
        self.assertEqual(printer.sources, [NO_SOURCE])
        self.assertEqual(printer.source, NO_SOURCE)
        self.assertIs(printer.current_options, listing)
        # there is no source to pass on
        printer.set_option('mode', 'Gray')
        self.assertEqual(printer.to_args(), ['--mode', 'Gray'])

    async def test_each_source_keeps_its_own_values(self):
        printer = await self.load('airscan')
        printer.set_option('resolution', 1200)
        printer.select_source('ADF')
        with self.assertRaises(ValueError):
            printer.set_option('resolution', 1200)     # not offered by the feeder
        printer.set_option('mode', 'Gray')

        self.assertEqual(printer.options_of('Flatbed')['resolution'].value, '1200')
        self.assertEqual(printer.options_of('Flatbed')['mode'].value, 'Color')
        self.assertEqual(printer.options_of('ADF')['mode'].value, 'Gray')
        self.assertEqual(printer.options_of('ADF')['resolution'].value, '300')

    async def test_settings_are_checked_against_the_current_source(self):
        printer = await self.load('airscan')
        printer.select_source('ADF')
        printer.set_option('resolution', 200)
        self.assertEqual(printer.to_args(), ['--source', 'ADF', '--resolution', '200'])
        with self.assertRaises(ValueError):
            printer.set_option('resolution', 1200)
        printer.select_source('Flatbed')
        self.assertEqual(printer.to_args(), ['--source', 'Flatbed'])    # nothing changed there yet
        printer.set_option('resolution', 1200)
        self.assertEqual(printer.to_args(), ['--source', 'Flatbed', '--resolution', '1200'])


if __name__ == '__main__':
    unittest.main()
