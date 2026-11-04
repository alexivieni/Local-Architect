---
name: type-hints
description: Add Python type hints that catch real mistakes without fighting the checker
when: type hints, type hint, type annotations, annotate, mypy, pyright, typing, Optional, TypedDict, type checker
files: *.py, *.pyi
---
# Python type hints

A hint is a promise to the reader and the checker. Make it true and make it useful.

- **Start at the edges.** Annotate public function arguments and return values first. Locals are usually inferred.
- **Say when None is possible.** `def find(id: int) -> User | None:`. Then every caller must handle the None, which is the point.
- **Use built-in generics** on Python 3.9+: `list[str]`, `dict[str, int]`, not `List` and `Dict` from typing.
- **Accept broad, return specific.** Take `Iterable[str]` or `Mapping[str, int]` as input; return a concrete `list` or `dict`.
- **Avoid `Any`.** It switches the checker off for everything it touches. If you must, keep it in one place and comment why.
- **Structured dicts** become a `TypedDict` or a `@dataclass`; fixed choices become `Literal["a", "b"]` or an `Enum`.
- **No runtime cost needed.** Add `from __future__ import annotations` if hints refer to classes defined later, and put
  imports used only for hints under `if TYPE_CHECKING:` to avoid circular imports.
- **Do not change behaviour while adding hints.** A hints-only change must not alter any logic.
- **Do not silence errors with `# type: ignore`** unless you add the error code and a reason.

Run the project's checker (mypy or pyright) if it has one, and fix what it reports in the files you touched.
