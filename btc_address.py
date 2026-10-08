"""Mainnet Bitcoin address checks: Base58Check and BIP173/BIP350 witness addresses.
Checksum validation detects typos; it does not establish ownership of an address.
"""
import hashlib


def normalize_btc_address(value):
    value = value.strip()
    if not 14 <= len(value) <= 90:
        raise ValueError('Enter a valid Bitcoin mainnet receiving address.')
    if value.lower().startswith('bc1'):
        if value != value.lower() and value != value.upper():
            raise ValueError('Bitcoin addresses cannot mix upper and lower case.')
        value = value.lower()
        alphabet = 'qpzry9x8gf2tvdw0s3jn54khce6mua7l'
        try:
            data = [alphabet.index(c) for c in value[3:]]
        except ValueError:
            raise ValueError('Invalid Bitcoin address characters.') from None
        if len(data) < 7 or data[0] > 16:
            raise ValueError('Invalid Bitcoin witness address.')
        checksum = 1
        for digit in [3, 3, 0, 2, 3] + data:  # Expanded mainnet HRP: bc
            top = checksum >> 25
            checksum = ((checksum & 0x1ffffff) << 5) ^ digit
            for i, gen in enumerate([0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]):
                if (top >> i) & 1:
                    checksum ^= gen
        if checksum != (1 if data[0] == 0 else 0x2bc830a3):
            raise ValueError('Bitcoin address checksum is invalid. Check the address and try again.')
        acc = bits = 0
        program = []
        for digit in data[1:-6]:
            acc = ((acc << 5) | digit) & 4095
            bits += 5
            if bits >= 8:
                bits -= 8
                program.append((acc >> bits) & 255)
        if bits >= 5 or (acc << (8 - bits)) & 255:
            raise ValueError('Invalid Bitcoin address padding.')
        if not 2 <= len(program) <= 40 or (data[0] == 0 and len(program) not in (20,32)):
            raise ValueError('Invalid Bitcoin witness program length.')
        return value
    alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    if len(value) > 35:
        raise ValueError('Enter a Bitcoin mainnet address, not a payment link.')
    number = 0
    try:
        for c in value:
            number = number * 58 + alphabet.index(c)
    except ValueError:
        raise ValueError('Invalid Bitcoin address characters.') from None
    raw = b'\0' * (len(value) - len(value.lstrip('1'))) + number.to_bytes((number.bit_length()+7)//8, 'big')
    if len(raw) != 25 or raw[0] not in (0,5) or hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4] != raw[-4:]:
        raise ValueError('Bitcoin address checksum or network is invalid. Use a mainnet BTC address.')
    return value
