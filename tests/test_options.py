import unittest
from pathlib import Path

from printer_backend.options import parse_help
from printer_backend.printer import Printer

FIXTURES = Path(__file__).parent / 'fixtures'


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
        self.printer = Printer('dev')
        self.printer.options = self.options

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
                self.assertEqual(self.printer.to_args({name: 'yes'}), [f'{flag}=yes'])
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
            if not option.active:
                continue
            with self.subTest(name):
                value = option.choices[-1] if option.choices is not None else option.max
                args = self.printer.to_args({name: value})
                if option.is_boolean:
                    self.assertEqual(args, [f'{option.flag}={value}'])
                else:
                    self.assertEqual(args, [option.flag, str(value)])


class HpaioOptionsTest(OptionTypesMixin, unittest.TestCase):
    fixture = FIXTURES / 'envy_6400_help.txt'
    enumerations = {
        'mode': ('--mode', ['Lineart', 'Gray', 'Color'], '', 'Color'),
        'resolution': ('--resolution', ['75', '100', '150', '200', '300'], 'dpi', '75'),
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
    fixture = FIXTURES / 'envy_6400_help_2.txt'
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
        self.printer = Printer('dev')
        self.printer.options = parse_help((FIXTURES / 'envy_6400_help.txt').read_text())

    def test_to_args(self):
        self.assertEqual(self.printer.to_args({'mode': 'Gray', 'resolution': 300}),
                         ['--mode', 'Gray', '--resolution', '300'])

    def test_rejects_bad_values(self):
        for settings in ({'resolution': '600'}, {'mode': 'Foo'}, {'x': 999}, {'nope': 1}, {'jpeg-quality': 50}):
            with self.assertRaises(ValueError, msg=settings):
                self.printer.validate(settings)

    def test_set_option_changes_the_value(self):
        self.printer.set_option('mode', 'Gray')
        self.printer.set_option('resolution', 300)
        self.assertEqual(self.printer.options['mode'].value, 'Gray')
        self.assertEqual(self.printer.options['resolution'].value, '300')

    def test_set_option_keeps_the_value_when_rejected(self):
        for name, value in (('mode', 'Foo'), ('resolution', '600'), ('nope', '1'), ('jpeg-quality', '50')):
            with self.subTest(name), self.assertRaises(ValueError):
                self.printer.set_option(name, value)
        self.assertEqual(self.printer.options['mode'].value, 'Color')
        self.assertEqual(self.printer.options['resolution'].value, '75')
        self.assertIsNone(self.printer.options['jpeg-quality'].value)


if __name__ == '__main__':
    unittest.main()
