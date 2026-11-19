"""Validate required column names in Polars DataFrames."""

from collections.abc import Iterable

import polars as pl


class MissingColumnsError(ValueError):
    """Raised when a DataFrame omits one or more required columns."""

    def __init__(self, missing_columns: Iterable[str]) -> None:
        self.missing_columns: tuple[str, ...] = tuple(missing_columns)
        names = ", ".join(repr(name) for name in self.missing_columns)
        super().__init__(f"Missing required columns: {names}")


class ParquetSchemaValidator:
    """Check that Polars DataFrames contain a configured set of columns."""

    def __init__(self, required_columns: list[str]) -> None:
        """Initialize the validator with required column names."""
        if isinstance(required_columns, (str, bytes)):
            raise TypeError("required_columns must be a list of column names")
        if not all(isinstance(name, str) for name in required_columns):
            raise TypeError("required_columns must contain only strings")

        # Preserve order while ensuring duplicate missing names appear once.
        self._required_columns: tuple[str, ...] = tuple(
            dict.fromkeys(required_columns)
        )

    def validate(self, dataframe: pl.DataFrame) -> None:
        """Validate columns in ``dataframe``, raising for any that are absent.

        Args:
            dataframe: The Polars DataFrame whose column names are checked.

        Raises:
            TypeError: If ``dataframe`` is not a Polars DataFrame.
            MissingColumnsError: If one or more configured columns are absent.
        """
        if not isinstance(dataframe, pl.DataFrame):
            raise TypeError("dataframe must be a polars.DataFrame")

        available_columns = set(dataframe.columns)
        missing_columns = [
            name for name in self._required_columns if name not in available_columns
        ]
        if missing_columns:
            raise MissingColumnsError(missing_columns)
