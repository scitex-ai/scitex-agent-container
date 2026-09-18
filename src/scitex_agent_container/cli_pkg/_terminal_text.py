"""Make untrusted scalar values inert before terminal rendering.

Rich's ``Text`` disables markup parsing, but it deliberately preserves terminal
control bytes.  External names, paths and remote stderr therefore need a second
boundary: remove OSC/CSI/ESC sequences and every C0/C1 control before a value is
placed in a table, narrow fallback, or footer.
"""

from __future__ import annotations

from typing import Any


def terminal_safe(value: Any) -> str:
    """Return ``value`` with terminal control sequences and bytes removed.

    Printable payload outside a control sequence is preserved literally,
    including Rich markup-looking brackets.  OSC and CSI payloads are discarded
    with their introducer/terminator so hyperlinks and colour commands cannot
    leak command text or become active terminal instructions.
    """
    text = str(value)
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        code = ord(text[index])

        # OSC: ESC ] ... BEL/ST, or its C1 single-byte form.
        if (
            code == 0x1B and index + 1 < length and text[index + 1] == "]"
        ) or code == 0x9D:
            index += 2 if code == 0x1B else 1
            while index < length:
                current = ord(text[index])
                if current in (0x07, 0x9C):
                    index += 1
                    break
                if current == 0x1B and index + 1 < length and text[index + 1] == "\\":
                    index += 2
                    break
                index += 1
            continue

        # CSI: ESC [ parameters/intermediates final, or C1 CSI.
        if (
            code == 0x1B and index + 1 < length and text[index + 1] == "["
        ) or code == 0x9B:
            index += 2 if code == 0x1B else 1
            while index < length:
                current = ord(text[index])
                index += 1
                if 0x40 <= current <= 0x7E:
                    break
            continue

        # A remaining ESC cannot become active once removed.  Consume its
        # optional one-byte final as well when it is an ordinary ESC sequence.
        if code == 0x1B:
            index += 1
            if index < length and 0x20 <= ord(text[index]) <= 0x7E:
                index += 1
            continue

        # C0, DEL and C1 are never allowed through an external scalar.  This
        # includes embedded newlines/tabs, which otherwise forge table/footer
        # lines even when markup parsing is disabled.
        if code < 0x20 or 0x7F <= code <= 0x9F:
            index += 1
            continue

        out.append(text[index])
        index += 1
    return "".join(out)


__all__ = ["terminal_safe"]
