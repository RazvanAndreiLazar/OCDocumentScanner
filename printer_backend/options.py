# For scanimage (sane-backends) 1.2.1-debian; backend version 1.2.1
from dataclasses import dataclass
import re

# bracket groups after the values that describe the option instead of giving its default
OPTION_TAGS = {'advanced', 'inactive', 'hardware', 'emulated', 'automatic'}

@dataclass
class ScanOption():
    name: str                       # 'mode' for '--mode', 'l' for '-l'
    flag: str                       # '--mode' / '-l'
    
    choices: list[str] | None = None # enumeration, e.g. ['Lineart', 'Gray', 'Color']
    
    min: float | None = None        # range, e.g. 0..215.9mm
    max: float | None = None
    unit: str = ''

    value: str | None = None
    active: bool = True
    is_boolean: bool = False        # '--flag[=(yes|no)]': value must be passed as --flag=yes

    def accepts(self, value) -> bool:
        value = str(value)
        if self.choices is not None:
            return value in self.choices
        if self.min is not None:
            try:
                return self.min <= float(value) <= self.max
            except ValueError:
                return False
        return True

    def __str__(self):
        # inactive option
        if not self.active:
            return self.name + ' (inactive)'
        # choice option
        if self.choices is not None:
            s = ' (' + ' | '.join(self.choices) + ')'
            idx = s.find(self.value)
            if idx != -1:
                s = s[:idx] + '[' + s[idx:idx+len(self.value)] + ']' + s[idx+len(self.value):]
            return self.name + s
        # range option
        if self.min is not None:
            return f'{self.name} ({self.value} \t{self.min}..{self.max}{self.unit})'
        # bool opotion
        if self.is_boolean:
            return f'{self.name} ({self.value})'

        raise AttributeError(f"Option '{self.name}' does not have an accepted type") 


# '    --mode Lineart|Gray|Color [Color]', '    --flag[=(yes|no)] [no]', '    -l 0..215.9mm [0]'
OPTION_LINE = re.compile(r'^ {4}(--?[\w-]+)(\[=\(yes\|no\)\])?(?: +(.*))?$')
RANGE = re.compile(r'^(-?\d+(?:\.\d+)?)\.\.(-?\d+(?:\.\d+)?)([a-zA-Z%]*)$')
NUMBER_WITH_UNIT = re.compile(r'^(-?\d+(?:\.\d+)?)([a-zA-Z%]*)$')


def _split_trailing_brackets(text: str) -> tuple[str, list[str]]:
    '''"0..100 [5] [advanced]" -> ("0..100", ["5", "advanced"])'''
    groups = []
    while (match := re.search(r'\s*\[([^\[\]]*)\]$', text)):
        groups.insert(0, match.group(1))
        text = text[:match.start()]
    return text.strip(), groups


def _parse_values(text: str, option: ScanOption):
    text = re.sub(r'\(in steps of [^)]*\)', '', text).strip()

    if (match := RANGE.match(text)):
        option.min, option.max, option.unit = float(match.group(1)), float(match.group(2)), match.group(3)
        return

    values = text.split('|')
    # the unit is only written on the last value: '75|100|300dpi'
    if len(values) > 1 and (match := NUMBER_WITH_UNIT.match(values[-1])) and match.group(2):
        option.unit = match.group(2)
        values[-1] = match.group(1)
    option.choices = values


def parse_help(text: str) -> dict[str, ScanOption]:
    '''
    Parses the device specific part of `scanimage -d DEVICE --help`.
    Takes only the text, so it can be tested without a scanner.
    '''
    start = re.search(r'^Options specific to device.*$', text, re.MULTILINE)
    if start:
        text = text[start.end():]
    # everything after this is the device list, which can hold garbage (see escl)
    for marker in ('Type ``scanimage', 'List of available devices'):
        text = text.split(marker)[0]

    options = {}
    for line in text.splitlines():
        if not (match := OPTION_LINE.match(line)):
            continue
        flag, boolean, rest = match.groups()
        option = ScanOption(name=flag.lstrip('-'), flag=flag, is_boolean=bool(boolean))

        rest, groups = _split_trailing_brackets(rest or '')
        defaults = [group for group in groups if group not in OPTION_TAGS]
        option.value = defaults[0] if defaults else None
        option.active = 'inactive' not in groups

        if boolean:
            option.choices = ['yes', 'no']
        elif rest:
            _parse_values(rest, option)
        options[option.name] = option

    return options
