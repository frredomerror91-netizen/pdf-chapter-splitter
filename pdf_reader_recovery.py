from __future__ import annotations

import re
from pypdf import PdfReader
from pypdf.generic import IndirectObject, NullObject
from pypdf.errors import PdfReadError


class RecoveryPdfReader(PdfReader):
    """Index orphaned object headers once, rather than scan the whole PDF per reference."""
    def __init__(self, *args, **kwargs) -> None:
        self._recovery_headers: dict[tuple[int, int], int] | None = None
        self.missing_objects: set[tuple[int, int]] = set()
        super().__init__(*args, **kwargs)

    def get_object(self, indirect_reference):
        if not isinstance(indirect_reference, IndirectObject):
            return super().get_object(indirect_reference)
        number, generation = indirect_reference.idnum, indirect_reference.generation
        cached = self.resolved_objects.get((generation, number))
        if cached is not None:
            return cached
        if (number in self.xref.get(generation, {})
            or (generation == 0 and number in self.xref_objStm)):
            return super().get_object(indirect_reference)
        if self._recovery_headers is None:
            position = self.stream.tell()
            try:
                self.stream.seek(0)
                contents = self.stream.read()
            finally:
                self.stream.seek(position)
            self._recovery_headers = {}
            for match in re.finditer(rb'(?<!\S)(\d+)\s+(\d+)\s+obj\b', contents):
                identity = (int(match.group(1)), int(match.group(2)))
                self._recovery_headers.setdefault(identity, match.start(1))
        offset = self._recovery_headers.get((number, generation))
        if offset is not None:
            self.xref.setdefault(generation, {})[number] = offset
            return super().get_object(indirect_reference)
        if self.strict:
            raise PdfReadError(f'Could not find object {number} {generation}.')
        self.missing_objects.add((number, generation))
        # Upstream retries cached None values. A PDF null is a stable missing reference.
        missing = NullObject()
        self.cache_indirect_object(generation, number, missing)
        return missing
