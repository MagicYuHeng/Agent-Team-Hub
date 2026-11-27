"""Shared HTTP request validation helpers."""


def required_text(body, key, limit):
    value = body.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{key} must be a non-empty string of at most {limit} characters.')
    value.encode('utf-8')
    return value
