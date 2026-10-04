"""Excel through COM in the Windows helper (DESIGN.md sections 6.3 and 6.9).

Native actions list a workbook's sheets, read and write ranges and save; the
agent reads each call's result, or why Excel refused it, in its next
observation. Extraction follows tier 1 checks for Excel: every open workbook is
saved as a copy with SaveCopyAs, which leaves the workbook itself untouched,
then openpyxl reads the values Excel last computed. A copy's bytes change with
every save, so only its sheet and cells are kept and an untouched workbook never
reads as a side effect. Workbooks arrive as files through task setup, so
materializing writes nothing. Everything works on objects shaped like Excel's
COM objects, so it is tested without Windows; COM's own error types are passed
in and become refusals.
"""

import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from rlworld.guest.protocol import NativeCallError
from rlworld.worlds.integrations import SPREADSHEET_SUFFIXES, cell_value, describe_file
from rlworld.worlds.model import World

NATIVE_METHODS = ("list_sheets", "read_range", "write_range", "save")
VOLATILE_FIELDS = ("size", "sha256")


def rows(cells: Any) -> list[list[Any]]:
    """COM returns one cell as a value and a range as rows of values."""
    if not isinstance(cells, (tuple, list)):
        return [[cell_value(cells)]]
    return [[cell_value(cell) for cell in row] for row in cells]


def written_rows(values: Any) -> tuple[tuple[Any, ...], ...]:
    """Values to write: equally long rows of cells; a text starting with = is a formula, as typed."""
    if (not isinstance(values, list) or not values or not all(isinstance(row, list) and row for row in values)
            or len({len(row) for row in values}) != 1):
        raise NativeCallError("values must be equally long rows of cells, such as [[1, 2], [3, \"=A1+B1\"]]")
    return tuple(tuple(row) for row in values)


def application_or_refusal(application: Callable[[], Any | None]) -> Any:
    excel = application()
    if excel is None or excel.Workbooks.Count == 0:
        raise NativeCallError("excel has no open workbook")
    return excel


def named(collection: Any, name: str, missing: str) -> Any:
    for item in collection:
        if item.Name == name:
            return item
    raise NativeCallError(missing)


class ExcelNative:
    """Excel's native API; application returns the running Excel, or None while none runs."""

    def __init__(self, application: Callable[[], Any | None], com_errors: tuple[type[Exception], ...] = ()) -> None:
        self.application = application
        self.com_errors = com_errors

    def call(self, method: str, arguments: Mapping[str, Any]) -> Any:
        if method not in NATIVE_METHODS:
            raise NativeCallError(f"excel has no method {method}; it has {', '.join(NATIVE_METHODS)}")
        try:
            return self.run(method, arguments)
        except self.com_errors as error:
            raise NativeCallError(f"excel refused {method}: {error}") from error

    def run(self, method: str, arguments: Mapping[str, Any]) -> Any:
        excel = application_or_refusal(self.application)
        workbook_name = arguments.get("workbook")
        workbook = excel.ActiveWorkbook if workbook_name is None else named(
            excel.Workbooks, workbook_name, f"no open workbook is named {workbook_name}")
        if method == "list_sheets":
            return {"workbook": workbook.Name, "sheets": [sheet.Name for sheet in workbook.Worksheets]}
        if method == "save":
            workbook.Save()
            return {"workbook": workbook.Name, "saved": True}
        sheet_name = arguments.get("sheet")
        sheet = workbook.ActiveSheet if sheet_name is None else named(
            workbook.Worksheets, sheet_name, f"{workbook.Name} has no sheet named {sheet_name}")
        address = arguments.get("range")
        if not isinstance(address, str) or not address:
            raise NativeCallError("a range call needs a range such as A1:B2")
        target = sheet.Range(address)
        if method == "read_range":
            return {"sheet": sheet.Name, "range": target.Address, "values": rows(target.Value),
                    "formulas": rows(target.Formula)}
        values = written_rows(arguments.get("values"))
        target = target.Resize(len(values), len(values[0]))
        target.Formula = values
        return {"sheet": sheet.Name, "range": target.Address}


class ExcelIntegration:
    """The excel app of a world: what the open workbooks hold, keyed by their path from the home."""

    app_name = "excel"

    def __init__(self, application: Callable[[], Any | None], home: Path) -> None:
        self.application = application
        self.home = home

    def materialize(self, world: World, target: Path) -> None:
        return None

    def workbook_key(self, workbook: Any) -> str:
        """A saved workbook by its path from the home, the way task files name it; a new one by its name."""
        path = Path(workbook.FullName)
        if not path.is_absolute():
            return workbook.Name
        return path.relative_to(self.home).as_posix() if path.is_relative_to(self.home) else path.as_posix()

    def extract(self, target: Path) -> dict[str, Any]:
        excel = self.application()
        workbooks: dict[str, Any] = {}
        if excel is None:
            return {"workbooks": workbooks}
        with tempfile.TemporaryDirectory(prefix="rlworld-excel-") as directory:
            for index, workbook in enumerate(excel.Workbooks):
                suffix = Path(workbook.Name).suffix.lower()
                copy_path = Path(directory) / f"copy{index}{suffix if suffix in SPREADSHEET_SUFFIXES else '.xlsx'}"
                workbook.SaveCopyAs(str(copy_path))
                description = describe_file(copy_path)
                workbooks[self.workbook_key(workbook)] = {field: value for field, value in description.items()
                                                         if field not in VOLATILE_FIELDS}
        return {"workbooks": workbooks}

    def reset(self, target: Path) -> None:
        """Closes every workbook without saving."""
        excel = self.application()
        if excel is not None:
            for workbook in list(excel.Workbooks):
                workbook.Close(False)
