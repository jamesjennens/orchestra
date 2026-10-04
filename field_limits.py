"""One wording for a text field that breaks its limit (kittrial-5bb.97).

A caller used to find a limit by failing against it: "summary: expected nonempty text
up to 1200 characters" said neither how long the text was nor, for a list, which item
was at fault, and some validators said only "requires a bounded reason". Every message
here names the field, the length it had and the limit, and an empty field and a field
of the wrong type each get their own sentence.
"""


def check_text(value, name, limit, empty=False, nul=True):
    """Raise ValueError unless `value` is text of at most `limit` characters.

    `empty` allows blank text; `nul` refuses a NUL character. Returns the value.
    """
    if not isinstance(value, str):
        raise ValueError('%s: expected text (limit %d characters)' % (name, limit))
    if len(value) > limit:
        raise ValueError('%s: %d characters, the limit is %d' % (name, len(value), limit))
    if nul and '\x00' in value:
        raise ValueError('%s: must not contain a NUL character (limit %d characters)' % (name, limit))
    if not empty and not value.strip():
        raise ValueError('%s: must not be empty (limit %d characters)' % (name, limit))
    return value


def describe(limits):
    """{field: '<= N characters'} for a help payload, from the validators' own numbers."""
    return {name: '<= %d characters' % limit for name, limit in limits.items()}
