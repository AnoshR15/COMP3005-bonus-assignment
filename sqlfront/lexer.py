"""SQL scanner. Reads one character at a time; no regular expressions.

Token kinds:
    KEYWORD   value is the keyword in upper case (keywords are case insensitive)
    IDENT     value is the name exactly as written (identifiers are case sensitive)
    INT       value is a Python int; an optional sign directly before the digits is part of it
    STRING    value is the text between the quotes, with '' turned into '
    OP        = <> < <= > >=      (maximal munch: <= and <> are one token, never < then = or >)
    ( ) , . ; *                   the kind is the character itself
    EOF

Every token carries pos = (line, column), both counted from 1.
"""
from dataclasses import dataclass
from .errors import SqlError

KEYWORDS = {
    'SELECT', 'DISTINCT', 'FROM', 'WHERE', 'GROUP', 'BY', 'HAVING', 'ORDER', 'ASC', 'DESC', 'LIMIT',
    'UNION', 'ALL', 'INTERSECT', 'EXCEPT', 'JOIN', 'ON', 'AS', 'AND', 'OR', 'NOT',
    'CREATE', 'TABLE', 'INSERT', 'INTO', 'VALUES', 'INT', 'VARCHAR',
    'COUNT', 'SUM', 'AVG', 'MIN', 'MAX',
}
PUNCTUATION = '(),.;*'
INT_MIN, INT_MAX = -2 ** 31, 2 ** 31 - 1          # INT is 4 bytes, signed


@dataclass(frozen=True)
class Token:
    kind: str
    value: object
    pos: tuple


def is_name_start(c):
    return c.isalpha() or c == '_'


def is_name_char(c):
    return c.isalnum() or c == '_'


class Lexer:

    def __init__(self, text):
        self.text = text
        self.i = 0
        self.line = 1
        self.column = 1

    def peek(self, ahead=0):
        j = self.i + ahead
        return self.text[j] if j < len(self.text) else ''

    def advance(self):
        c = self.text[self.i]
        self.i += 1
        if c == '\n':
            self.line += 1
            self.column = 1
        else:
            self.column += 1
        return c

    def scan(self):
        tokens = []
        while True:
            self.skip_blanks_and_comments()
            pos = (self.line, self.column)
            c = self.peek()
            if c == '':
                tokens.append(Token('EOF', None, pos))
                return tokens
            if is_name_start(c):
                tokens.append(self.word(pos))
            elif c.isdigit() or (c in '+-' and self.peek(1).isdigit()):
                tokens.append(self.number(pos))
            elif c == "'":
                tokens.append(self.string(pos))
            elif c in '<>=':
                tokens.append(self.operator(pos))
            elif c in PUNCTUATION:
                tokens.append(Token(self.advance(), c, pos))
            else:
                raise SqlError('lexical', f"unexpected character '{c}'", pos)

    def skip_blanks_and_comments(self):
        while True:
            c = self.peek()
            if c != '' and c.isspace():
                self.advance()
            elif c == '-' and self.peek(1) == '-':         # comment: -- to the end of the line
                while self.peek() not in ('', '\n'):
                    self.advance()
            else:
                return

    def word(self, pos):
        start = self.i
        while self.peek() != '' and is_name_char(self.peek()):
            self.advance()
        text = self.text[start:self.i]
        if text.upper() in KEYWORDS:
            return Token('KEYWORD', text.upper(), pos)
        return Token('IDENT', text, pos)

    def number(self, pos):
        start = self.i
        self.advance()                                     # a sign or the first digit
        while self.peek().isdigit():
            self.advance()
        if self.peek() != '' and (is_name_char(self.peek()) or self.peek() == '.' and self.peek(1).isdigit()):
            end = self.i                                   # quote all of it: 1.5, not 1.
            while end < len(self.text) and (is_name_char(self.text[end]) or self.text[end] == '.'):
                end += 1
            raise SqlError('lexical', f"invalid number '{self.text[start:end]}'; numbers are integers", pos)
        value = int(self.text[start:self.i])
        if not INT_MIN <= value <= INT_MAX:
            raise SqlError('lexical', f'integer {value} does not fit in a 4-byte INT', pos)
        return Token('INT', value, pos)

    def string(self, pos):
        self.advance()                                     # the opening quote
        out = []
        while True:
            c = self.peek()
            if c == '':
                raise SqlError('lexical', 'unterminated string', pos)
            self.advance()
            if c == "'":
                if self.peek() == "'":                     # '' inside a string is one quote
                    self.advance()
                    out.append("'")
                    continue
                return Token('STRING', ''.join(out), pos)
            out.append(c)

    def operator(self, pos):
        c = self.advance()
        nxt = self.peek()
        if c == '<' and nxt in ('=', '>') or c == '>' and nxt == '=':
            return Token('OP', c + self.advance(), pos)
        return Token('OP', c, pos)


def scan(text):
    return Lexer(text).scan()
