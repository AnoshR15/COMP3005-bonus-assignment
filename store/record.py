"""Record format: the field encodings one after another, in schema order.
No record header and no null bitmap, because there are no nulls.

    INT         4 bytes, signed, two's complement, little endian
    VARCHAR(n)  2-byte unsigned length L, then L bytes of UTF-8; L <= n and n <= 255
    REAL        8 bytes, IEEE double; never a table column, only the result of AVG in a temp file
    INT, wide   8 bytes, signed; never a table column, only the result of SUM in a temp file.
                A SUM of 4-byte values can pass 2,147,483,647, and a temporary file must be able
                to hold it. It is written as (INT, 8) in a column list; a table's INT is (INT, None).

Schema (a INT, b VARCHAR(5), c INT) and row (42, 'bob', 7):

    a   2A 00 00 00          4 bytes
    b   03 00 62 6F 62       2 + 3 bytes
    c   07 00 00 00          4 bytes     record length 13

A record has a fixed length only when every column is INT. Nothing here assumes it.
"""
import struct

INT, VARCHAR, REAL = 'INT', 'VARCHAR', 'REAL'
INT_MIN, INT_MAX = -2 ** 31, 2 ** 31 - 1
_LENGTH = struct.Struct('<H')
WIDE = 8                                    # the size that marks an 8-byte INT


class RecordError(ValueError):
    pass


def number_format(kind, size):
    """The struct letter of a fixed-width column: i is 4 bytes, q is 8, d is a double."""
    return 'd' if kind == REAL else 'q' if size == WIDE else 'i'


class RecordCodec:
    """Encodes and decodes rows of one schema. `columns` is a list of (kind, size)."""

    def __init__(self, columns):
        self.columns = [(kind, size) for kind, size in columns]
        self.kinds = [kind for kind, _ in self.columns]
        # one struct per column that is not a VARCHAR, None for those that are
        self.numbers = [None if kind == VARCHAR else struct.Struct('<' + number_format(kind, size))
                        for kind, size in self.columns]
        self.fixed = None
        if VARCHAR not in self.kinds:           # every column has a fixed width: one struct does it
            self.fixed = struct.Struct('<' + ''.join(number_format(k, s) for k, s in self.columns))

    def size(self, row):
        """The encoded size of a row, without encoding it."""
        if self.fixed:
            return self.fixed.size
        total = 0
        for number, value in zip(self.numbers, row):
            if number is None:
                total += 2 + (len(value) if value.isascii() else len(value.encode('utf-8')))
            else:
                total += number.size
        return total

    def encode(self, row):
        try:
            if self.fixed:
                return self.fixed.pack(*row)
            parts = []
            for number, value in zip(self.numbers, row):
                if number is None:
                    data = value.encode('utf-8')
                    parts.append(_LENGTH.pack(len(data)))
                    parts.append(data)
                else:
                    parts.append(number.pack(value))
            return b''.join(parts)
        except struct.error:
            raise RecordError(f'a value of the row {row!r} does not fit its column') from None

    def decode(self, data):
        if self.fixed:
            return self.fixed.unpack(data)
        row = []
        at = 0
        for number in self.numbers:
            if number is None:
                length = _LENGTH.unpack_from(data, at)[0]
                row.append(data[at + 2:at + 2 + length].decode('utf-8'))
                at += 2 + length
            else:
                row.append(number.unpack_from(data, at)[0])
                at += number.size
        return tuple(row)

    def check(self, row, names=None):
        """Raise RecordError unless the row fits the schema: arity, types, ranges, lengths."""
        if len(row) != len(self.columns):
            raise RecordError(f'the table has {len(self.columns)} columns but the row has {len(row)} values')
        for i, ((kind, size), value) in enumerate(zip(self.columns, row)):
            name = f'column {names[i]}' if names else f'column {i + 1}'
            if kind == INT:
                if not isinstance(value, int) or isinstance(value, bool):
                    raise RecordError(f'{name} is INT but the value is {value!r}')
                if not INT_MIN <= value <= INT_MAX:
                    raise RecordError(f'{name}: {value} does not fit in a 4-byte INT')
            elif kind == VARCHAR:
                if not isinstance(value, str):
                    raise RecordError(f'{name} is VARCHAR but the value is {value!r}')
                length = len(value.encode('utf-8'))
                if length > size:
                    raise RecordError(f'{name} is VARCHAR({size}) but the value is {length} bytes long')
