"""Loader for relation definition files (Section 4.1 of the assignment).

    // employees and their departments
    Employees (EID, Name, Age, DID) = {
      E1, John, 32, D1
      E2, Alice, 28, D2
    }

A value is a number or a string. A bare value that scans as a number is a number; anything else
is a string. A string containing a comma, space, parenthesis or quote must be single-quoted, and
'' inside quotes is one literal quote. Everything here is scanned character by character.
An attribute's type is taken from its values, so a relation needs at least one tuple.
"""
from .errors import DataError
from .operators import Relation, SchemaAttr, type_of, canonical_row


def _strip_comment(line):
    """Remove a // comment, but not a // that sits inside a quoted string."""
    out = []
    i = 0
    quoted = False
    while i < len(line):
        c = line[i]
        if c == "'":
            out.append(c)
            i += 1
            if quoted and i < len(line) and line[i] == "'":   # '' inside a string
                out.append("'")
                i += 1
            else:
                quoted = not quoted
            continue
        if not quoted and line.startswith('//', i):
            break
        out.append(c)
        i += 1
    return ''.join(out).strip()


def _find_unquoted(s, target):
    """Index of the first `target` character outside quotes, or -1."""
    quoted = False
    i = 0
    while i < len(s):
        if s[i] == "'":
            if quoted and i + 1 < len(s) and s[i + 1] == "'":
                i += 2
                continue
            quoted = not quoted
        elif s[i] == target and not quoted:
            return i
        i += 1
    return -1


def _split_csv(s, line_no):
    """Split on the commas that are outside quotes."""
    parts = []
    buf = []
    quoted = False
    i = 0
    while i < len(s):
        c = s[i]
        if c == "'":
            buf.append(c)
            i += 1
            if quoted and i < len(s) and s[i] == "'":
                buf.append("'")
                i += 1
            else:
                quoted = not quoted
            continue
        if c == ',' and not quoted:
            parts.append(''.join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    if quoted:
        raise DataError(f'Unterminated string on line {line_no}')
    parts.append(''.join(buf).strip())
    return parts


def _number(s):
    """int or float when s is written as a number, otherwise None. Scans the characters, no regex."""
    i = 1 if s[:1] == '-' else 0
    digits = 0
    while i < len(s) and s[i].isdigit():
        i += 1
        digits += 1
    if digits == 0:
        return None
    if i == len(s):
        return int(s)
    if s[i] != '.':
        return None
    i += 1
    fraction = 0
    while i < len(s) and s[i].isdigit():
        i += 1
        fraction += 1
    return float(s) if fraction and i == len(s) else None


def _value(s, line_no):
    if not s:
        raise DataError(f'Missing value on line {line_no}')
    if s[0] == "'":
        if len(s) < 2 or s[-1] != "'":
            raise DataError(f'Unterminated string on line {line_no}')
        out = []
        i = 1
        while i < len(s) - 1:
            if s[i] == "'":
                if i + 1 < len(s) - 1 and s[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                raise DataError(f'Stray quote inside a string on line {line_no}')
            out.append(s[i])
            i += 1
        return ''.join(out)
    number = _number(s)
    return s if number is None else number


def _parse_header(line, line_no):
    """Name (attr, attr, ...) = {   ->   (name, [attrs])"""
    eq = line.find('=')
    if eq < 0:
        raise DataError(f"Expected '=' in the relation header on line {line_no}")
    left = line[:eq].strip()
    if not line[eq + 1:].strip().startswith('{'):
        raise DataError(f"Expected '{{' after '=' on line {line_no}")
    lp = left.find('(')
    rp = left.rfind(')')
    if lp <= 0 or rp < lp:
        raise DataError(f'Invalid relation header on line {line_no}: expected Name (attr, ...) = {{')
    name = left[:lp].strip()
    if not name or any(ch.isspace() for ch in name):
        raise DataError(f'Invalid relation name on line {line_no}')
    attrs = _split_csv(left[lp + 1:rp], line_no)
    if not attrs or any(not a for a in attrs):
        raise DataError(f'Invalid attribute list on line {line_no}')
    for a in attrs:
        if attrs.count(a) > 1:
            raise DataError(f"Attribute '{a}' is listed twice in relation {name} on line {line_no}")
    return name, attrs


def _parse_tuple(text, attrs, line_no):
    values = [_value(v, line_no) for v in _split_csv(text, line_no)]
    if len(values) != len(attrs):
        raise DataError(f'Tuple on line {line_no} has {len(values)} values; '
                        f'the relation has {len(attrs)} attributes')
    return tuple(values)


def load_relations(path):
    """Read a file of relation definitions. Returns {name: Relation}."""
    with open(path, encoding='utf8') as f:
        raw = f.readlines()
    out = {}
    i = 0
    while i < len(raw):
        line = _strip_comment(raw[i])
        i += 1
        if not line:
            continue
        name, attrs = _parse_header(line, i)
        header_line = i
        rows = []                                 # a bag: duplicate tuples are kept
        rest = line[line.find('{') + 1:]          # text after '{' on the header line, usually empty
        while True:
            text = _strip_comment(rest)
            close = _find_unquoted(text, '}')
            if close >= 0:
                before = text[:close].strip()
                if before:
                    rows.append(_parse_tuple(before, attrs, i))
                break
            if text:
                rows.append(_parse_tuple(text, attrs, i))
            if i >= len(raw):
                raise DataError(f"Relation '{name}' starting on line {header_line} is never closed with '}}'")
            rest = raw[i]
            i += 1
        if not rows:
            raise DataError(f"Relation '{name}' has no tuples; attribute types are taken from the data")
        if name in out:
            raise DataError(f"Relation '{name}' is defined twice")
        types = []
        for col, a in enumerate(attrs):
            kinds = {type_of(r[col]) for r in rows}
            if len(kinds) > 1:
                raise DataError(f"Attribute '{a}' of {name} mixes numbers and strings")
            types.append(kinds.pop())
        out[name] = Relation(name, [SchemaAttr(name, a, t) for a, t in zip(attrs, types)],
                             [canonical_row(r) for r in rows])
    return out
