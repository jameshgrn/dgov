import re


def truncate(text: str, max_len: int = 80) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def slug_safe(name: str) -> str:
    return re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")
