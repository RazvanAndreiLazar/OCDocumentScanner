import base64
import re


def device_to_id(device: str) -> str:
    '''Device names hold '/', '?' and ':' (hpaio:/net/ENVY?ip=...), so URLs carry this instead.'''
    return base64.urlsafe_b64encode(device.encode()).decode().rstrip('=')


def id_to_device(device_id: str) -> str:
    '''Raises ValueError if the id is not one that device_to_id made.'''
    if not re.fullmatch(r'[A-Za-z0-9_-]+', device_id):  # b64decode would silently skip other characters
        raise ValueError(f"'{device_id}' is not a printer id")
    try:
        return base64.urlsafe_b64decode(device_id + '=' * (-len(device_id) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        raise ValueError(f"'{device_id}' is not a printer id") from None


def device_name(device: str) -> str:
    '''A name to show for a device: hpaio:/net/ENVY_6400_series?ip=... is 'ENVY 6400 series'. Others stay as they are.'''
    if match := re.match(r'hpaio:/\w+/([^?]+)', device):
        return match.group(1).replace('_', ' ')
    if match := re.match(r'airscan:\w+:(.+)', device):
        return match.group(1)
    return device
