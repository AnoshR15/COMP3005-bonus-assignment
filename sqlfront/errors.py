"""The one error type of the SQL front end.

Every error has a category and a position, and prints in one format:

    error: <category>: <message> at line L, column C
"""

CATEGORIES = ('lexical', 'syntax', 'name', 'ambiguity', 'type', 'aggregate', 'schema')


class SqlError(Exception):

    def __init__(self, category, message, pos):
        assert category in CATEGORIES
        self.category = category
        self.message = message
        self.line, self.column = pos
        super().__init__(f'error: {category}: {message} at line {self.line}, column {self.column}')
