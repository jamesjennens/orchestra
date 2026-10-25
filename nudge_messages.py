"""The fixed lane nudge texts and their version (kittrial-5bb.415; design kittrial-5bb.409 section 2).

Library only. The server sends a carrier a key (``resume`` or ``restart``) and ``MESSAGE_SET``; the
carrier types the text from ITS OWN copy of this table. A key or a version it does not know is answered
``unsupported`` and nothing is typed, so neither a coordinator, a note nor a mistaken server can make a
lane receive anything outside these two lines. A change of text is a kit release and bumps
``MESSAGE_SET``.

Each text is one line of plain ASCII: no markup, no path, no name, no id and no control character.
"""

MESSAGE_SET = 'nudge-messages-v1'

_TAIL = ('Read your own notes, run `work --mine`, read the newest comments of each task that is your move, '
         'and carry on. If nothing is your move, wait with the kit as guidance says.')

TEXTS = {
    'resume': 'Orchestra: a coordinator asked you to check in. ' + _TAIL,
    'restart': 'Orchestra: this session was restarted at a coordinator\'s request. ' + _TAIL,
}


def text_for(key, message_set):
    """The line to type for ``key`` under ``message_set``, or ``None`` when either is not known here."""
    if message_set != MESSAGE_SET or not isinstance(key, str):
        return None
    return TEXTS.get(key)
