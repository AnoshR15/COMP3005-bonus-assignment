from dataclasses import dataclass
from .errors import LexError

KEYWORDS = {"select", "project", "rename", "distinct", "join", "union", "intersect", "minus", "times", "and", "or", "not"}

@dataclass(frozen=True)
class Token:
    kind: str
    value: object
    pos: int

class Tokenizer:
    def __init__(self, text: str):
        self.text = text
        self.i = 0
        self.tokens = []

    def scan(self):
        while self.i < len(self.text):
            c = self.text[self.i]
            if c.isspace():
                self.i += 1; continue
            if self.text.startswith('//', self.i):
                while self.i < len(self.text) and self.text[self.i] != '\n': self.i += 1
                continue
            pos = self.i
            if c.isalpha() or c == '_':
                self.i += 1
                while self.i < len(self.text) and (self.text[self.i].isalnum() or self.text[self.i] == '_'):
                    self.i += 1
                s = self.text[pos:self.i]
                # Keywords are recognized by parser context, so keyword-spelled attributes remain usable.
                self.tokens.append(Token('WORD', s, pos)); continue
            if c.isdigit() or (c == '-' and self.i + 1 < len(self.text) and self.text[self.i+1].isdigit()):
                self.i += 1
                while self.i < len(self.text) and self.text[self.i].isdigit(): self.i += 1
                if self.i < len(self.text) and self.text[self.i] == '.':
                    self.i += 1
                    if self.i >= len(self.text) or not self.text[self.i].isdigit():
                        raise LexError(f"Invalid number at position {pos}")
                    while self.i < len(self.text) and self.text[self.i].isdigit(): self.i += 1
                    value = float(self.text[pos:self.i])
                else:
                    value = int(self.text[pos:self.i])
                self.tokens.append(Token('NUMBER', value, pos)); continue
            if c == "'":
                self.i += 1; out = []
                while self.i < len(self.text):
                    if self.text[self.i] == "'":
                        if self.i + 1 < len(self.text) and self.text[self.i+1] == "'":
                            out.append("'"); self.i += 2; continue
                        self.i += 1; break
                    out.append(self.text[self.i]); self.i += 1
                else:
                    raise LexError(f"Unterminated string at position {pos}")
                self.tokens.append(Token('STRING', ''.join(out), pos)); continue
            if c in '[](),{}':
                self.tokens.append(Token(c, c, pos)); self.i += 1; continue
            if c in '<>!=':
                self.i += 1
                if self.i < len(self.text) and self.text[self.i] == '=' and c in '<>!':
                    self.i += 1; self.tokens.append(Token('OP', c+'=', pos))
                elif c == '<' and self.i < len(self.text) and self.text[self.i] == '>':
                    self.i += 1; self.tokens.append(Token('OP', '<>', pos))      # SQL's spelling of !=
                elif c == '!':
                    raise LexError(f"Expected '=' after '!' at position {pos}")
                else:
                    self.tokens.append(Token('OP', c, pos))
                continue
            if c == '=':
                self.tokens.append(Token('OP', '=', pos)); self.i += 1; continue
            if c == '.':
                self.tokens.append(Token('.', '.', pos)); self.i += 1; continue
            raise LexError(f"Unexpected character {c!r} at position {pos}")
        self.tokens.append(Token('EOF', None, len(self.text)))
        return self.tokens
