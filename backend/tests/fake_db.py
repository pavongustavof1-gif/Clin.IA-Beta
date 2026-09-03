# backend/tests/fake_db.py
# A tiny in-memory stand-in for the PostgREST surface app.py's _sb_get/
# _sb_patch/_sb_delete talk to. Parses just enough of the real query
# shapes this codebase actually emits (confirmed by reading every call
# site before writing this: eq., in.(), and one and.(...) composite in
# admin_sessions) to drive the real route/authorization code against
# synthetic data — never a real Supabase project, no network, no PHI.
#
# This is deliberately NOT a general PostgREST clone. It exists only to
# make the authorization test suite's routes behave correctly against
# fixture data; if a route starts using a filter shape this doesn't
# understand, extend _match_row rather than special-casing the route.

from __future__ import annotations
import urllib.parse


class FakeDB:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {
            'usuarios': [],
            'clinicas': [],
            'sesiones': [],
            'trabajos': [],
            'lecturas_sesion': [],
        }

    def seed(self, table: str, rows: list[dict]) -> None:
        self.tables[table] = [dict(r) for r in rows]

    def add(self, table: str, row: dict) -> None:
        self.tables.setdefault(table, []).append(dict(row))

    # ── path parsing ──────────────────────────────────────────────────

    @staticmethod
    def _parse_path(path: str) -> tuple[str, dict[str, list[str]]]:
        assert path.startswith('/rest/v1/'), f"not a PostgREST path: {path}"
        rest = path[len('/rest/v1/'):]
        if '?' in rest:
            table, qs = rest.split('?', 1)
        else:
            table, qs = rest, ''
        params: dict[str, list[str]] = {}
        for part in qs.split('&'):
            if not part or '=' not in part:
                continue
            k, v = part.split('=', 1)
            params.setdefault(k, []).append(v)
        return table, params

    @staticmethod
    def _match_clause(row: dict, field: str, op: str, raw_value: str) -> bool:
        value = urllib.parse.unquote(raw_value)
        actual = row.get(field)
        if op == 'eq':
            return str(actual) == value
        if op == 'gte':
            return actual is not None and str(actual) >= value
        if op == 'lte':
            return actual is not None and str(actual) <= value
        if op == 'in':
            inner = value.strip('()')
            allowed = [x for x in inner.split(',') if x]
            return str(actual) in allowed
        # Unrecognized operator — fail loud rather than silently match
        # everything, which would make a test pass for the wrong reason.
        raise NotImplementedError(f"FakeDB: unhandled PostgREST operator '{op}' on field '{field}'")

    def _match_row(self, row: dict, params: dict[str, list[str]]) -> bool:
        for key, values in params.items():
            if key in ('select', 'order', 'limit', 'offset'):
                continue
            for v in values:
                if key == 'and':
                    inner = urllib.parse.unquote(v).strip('()')
                    for clause in inner.split(','):
                        field, op, val = clause.split('.', 2)
                        if not self._match_clause(row, field, op, val):
                            return False
                    continue
                if '.' not in v:
                    raise NotImplementedError(f"FakeDB: unrecognized filter value '{v}' for field '{key}'")
                op, val = v.split('.', 1)
                if not self._match_clause(row, key, op, val):
                    return False
        return True

    # ── operations mirroring _sb_get/_sb_patch/_sb_delete ───────────────

    def get(self, path: str) -> list[dict]:
        table, params = self._parse_path(path)
        rows = self.tables.get(table, [])
        result = [dict(r) for r in rows if self._match_row(r, params)]
        if 'limit' in params:
            result = result[:int(params['limit'][0])]
        return result

    def patch(self, path: str, body: dict) -> bool:
        table, params = self._parse_path(path)
        rows = self.tables.get(table, [])
        matched = False
        for row in rows:
            if self._match_row(row, params):
                row.update(body)
                matched = True
        return matched or True  # PostgREST PATCH succeeds even matching 0 rows

    def delete(self, path: str) -> bool:
        table, params = self._parse_path(path)
        rows = self.tables.get(table, [])
        self.tables[table] = [r for r in rows if not self._match_row(r, params)]
        return True

    def post(self, path: str, body: dict) -> dict:
        table, _ = self._parse_path(path)
        row = dict(body)
        self.tables.setdefault(table, []).append(row)
        return row
