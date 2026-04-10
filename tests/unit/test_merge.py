"""Unit tests for bioimageflow.merge tools."""

import pandas as pd
import pytest

from bioimageflow_core.arguments import Arguments
from bioimageflow_common_tools import InnerJoin, CrossJoin, JoinOnColumn, Concat, Collect


@pytest.fixture
def df_a():
    return pd.DataFrame({"x": [1, 2, 3]}, index=["0", "1", "2"])


@pytest.fixture
def df_b():
    return pd.DataFrame({"y": [10, 20, 30]}, index=["0", "1", "2"])


class TestInnerJoin:

    def test_single_df_returns_copy(self, df_a):
        tool = InnerJoin()
        result = tool.merge_dataframes([df_a], Arguments())
        pd.testing.assert_frame_equal(result, df_a)
        assert result is not df_a

    def test_two_dfs_join_on_index(self, df_a, df_b):
        tool = InnerJoin()
        result = tool.merge_dataframes([df_a, df_b], Arguments())
        assert list(result.columns) == ["x", "y"]
        assert len(result) == 3

    def test_duplicate_columns_deduplicated(self):
        df1 = pd.DataFrame({"col": [1, 2]}, index=["0", "1"])
        df2 = pd.DataFrame({"col": [3, 4]}, index=["0", "1"])
        tool = InnerJoin()
        result = tool.merge_dataframes([df1, df2], Arguments())
        assert "col" in result.columns
        assert "col__bif_dup" not in result.columns

    def test_empty_input(self):
        tool = InnerJoin()
        result = tool.merge_dataframes([], Arguments())
        assert len(result) == 0


class TestCrossJoin:

    def test_cross_product(self):
        df1 = pd.DataFrame({"a": [1, 2]}, index=["0", "1"])
        df2 = pd.DataFrame({"b": [10, 20]}, index=["0", "1"])
        tool = CrossJoin()
        result = tool.merge_dataframes([df1, df2], Arguments(suffixes=("_left", "_right")))
        assert len(result) == 4
        assert "a" in result.columns
        assert "b" in result.columns


class TestJoinOnColumn:

    def test_join_on_shared_column(self):
        df1 = pd.DataFrame({"key": ["a", "b"], "val1": [1, 2]})
        df2 = pd.DataFrame({"key": ["a", "b"], "val2": [10, 20]})
        tool = JoinOnColumn()
        result = tool.merge_dataframes(
            [df1, df2],
            Arguments(join_column="key", how="inner", suffixes=("_left", "_right")),
        )
        assert len(result) == 2
        assert "val1" in result.columns
        assert "val2" in result.columns


class TestConcat:

    def test_vertical_concat(self):
        df1 = pd.DataFrame({"x": [1, 2]})
        df2 = pd.DataFrame({"x": [3, 4]})
        tool = Concat()
        result = tool.merge_dataframes([df1, df2], Arguments())
        assert len(result) == 4
        assert list(result["x"]) == [1, 2, 3, 4]

    def test_preserves_index(self):
        df1 = pd.DataFrame({"x": [1]}, index=["a"])
        df2 = pd.DataFrame({"x": [2]}, index=["b"])
        tool = Concat()
        result = tool.merge_dataframes([df1, df2], Arguments())
        assert list(result.index) == ["a", "b"]


class TestCollect:

    def test_collect_is_passthrough(self):
        tool = Collect()
        # Collect uses default merge (inner join) and default transform (identity)
        df = pd.DataFrame({"a": [1], "b": [2]}, index=["0"])
        result = tool.transform(df, Arguments())
        pd.testing.assert_frame_equal(result, df)
