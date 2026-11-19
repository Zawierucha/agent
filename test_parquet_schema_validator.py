"""Tests for validating required columns in Polars DataFrames."""

import polars as pl
import pytest

from parquet_schema_validator import MissingColumnsError, ParquetSchemaValidator


def test_validate_accepts_dataframe_with_all_required_columns() -> None:
    dataframe = pl.DataFrame({"id": [1], "name": ["Ada"], "age": [37]})
    validator = ParquetSchemaValidator(["id", "name"])

    assert validator.validate(dataframe) is None


def test_validate_reports_one_missing_column() -> None:
    dataframe = pl.DataFrame({"id": [1]})
    validator = ParquetSchemaValidator(["id", "name"])

    with pytest.raises(MissingColumnsError) as error_info:
        validator.validate(dataframe)

    assert error_info.value.missing_columns == ("name",)


def test_validate_reports_multiple_missing_columns_in_required_order() -> None:
    dataframe = pl.DataFrame({"id": [1]})
    validator = ParquetSchemaValidator(["id", "name", "age"])

    with pytest.raises(MissingColumnsError) as error_info:
        validator.validate(dataframe)

    assert error_info.value.missing_columns == ("name", "age")


def test_validate_accepts_empty_required_column_list() -> None:
    dataframe = pl.DataFrame({"id": [1]})
    validator = ParquetSchemaValidator([])

    assert validator.validate(dataframe) is None


def test_missing_column_error_message_names_missing_columns() -> None:
    dataframe = pl.DataFrame({"id": [1]})
    validator = ParquetSchemaValidator(["name", "age"])

    with pytest.raises(MissingColumnsError) as error_info:
        validator.validate(dataframe)

    message = str(error_info.value)
    assert "Missing required columns" in message
    assert "name" in message
    assert "age" in message


def test_duplicate_required_names_are_reported_only_once() -> None:
    dataframe = pl.DataFrame({"id": [1]})
    validator = ParquetSchemaValidator(["missing", "missing"])

    with pytest.raises(MissingColumnsError) as error_info:
        validator.validate(dataframe)

    assert error_info.value.missing_columns == ("missing",)


def test_validate_rejects_non_polars_dataframe() -> None:
    validator = ParquetSchemaValidator(["id"])

    with pytest.raises(TypeError, match="polars.DataFrame"):
        validator.validate({"id": [1]})  # type: ignore[arg-type]
