import json
import os


def read_json(path: str, default):
    '''The contents of a json file, or default if there is no such file.'''
    try:
        with open(path, encoding='utf-8') as file:
            return json.load(file)
    except FileNotFoundError:
        return default


def write_json(path: str, data):
    # written next to the file and moved over it: a crash can't leave half of a file behind
    temporary = f'{path}.tmp'
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(temporary, 'w', encoding='utf-8') as file:
        json.dump(data, file, indent=2)
    os.replace(temporary, path)
