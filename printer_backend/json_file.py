import json
import logging
import os

log = logging.getLogger(__name__)


def read_json(path: str, default, build=None):
    '''
    The contents of a json file, or default if there is no such file.

    A file that can't be used (not valid json, or build(data) rejects it with ValueError, KeyError or
    TypeError, e.g. after the fields of a saved class changed) is moved aside to '<path>.broken' and
    default is returned, with a warning. Otherwise the app would fail at every start. With build, the
    result is build(data).
    '''
    try:
        with open(path, encoding='utf-8') as file:
            data = json.load(file)
        return data if build is None else build(data)
    except FileNotFoundError:
        return default
    except (ValueError, KeyError, TypeError) as error:    # JSONDecodeError and UnicodeDecodeError are ValueErrors
        set_aside(path, error)
        return default


def set_aside(path: str, reason: object):
    '''Moves a file that can't be used to '<path>.broken' (replacing an older one), so it can be looked at and is not overwritten.'''
    broken = f'{path}.broken'
    os.replace(path, broken)
    log.warning("Could not use '%s' (%s: %s): moved it to '%s' and started without it",
                path, type(reason).__name__, reason, broken)


def write_json(path: str, data):
    # written next to the file and moved over it: a crash can't leave half of a file behind
    temporary = f'{path}.tmp'
    folder = os.path.dirname(path) or '.'
    os.makedirs(folder, exist_ok=True)
    with open(temporary, 'w', encoding='utf-8') as file:
        json.dump(data, file, indent=2)
        file.flush()
        os.fsync(file.fileno())     # on disk before the move: after a power loss the new file could otherwise be empty
    os.replace(temporary, path)
    directory = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(directory)         # so the move itself survives, too
    finally:
        os.close(directory)
