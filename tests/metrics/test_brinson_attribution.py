"""Tests for Brinson performance attribution."""

from __future__ import annotations

import numpy as np
import pytest

from core.metrics.brinson_attribution import (
    brinson_decompose,
    brinson_multi_period,
)


class TestBrinsonDecompose:
    def test_perfect_replication_zero_active(self):
        """Same weights and returns → zero active return."""
        result = brinson_decompose(
            sectors=["A", "B", "C"],
            port_weights=[0.4, 0.4, 0.2],
            bench_weights=[0.4, 0.4, 0.2],
            port_returns=[0.02, 0.01, -0.005],
            bench_returns=[0.02, 0.01, -0.005],
        )
        assert result.active_return == pytest.approx(0.0)
        assert result.total_allocation == pytest.approx(0.0)
        assert result.total_selection == pytest.approx(0.0)

    def test_overweight_winner(self):
        """Overweight a sector that beats benchmark → positive allocation."""
        result = brinson_decompose(
            sectors=["A", "B"],
            port_weights=[0.8, 0.2],
            bench_weights=[0.5, 0.5],
            port_returns=[0.05, 0.01],
            bench_returns=[0.05, 0.01],
        )
        # A's benchmark return is 5%, overweight by 30% → +0.015 allocation
        assert result.total_allocation > 0
        assert result.total_selection == pytest.approx(0.0)

    def test_better_stock_picking(self):
        """Same weights, better returns → positive selection."""
        result = brinson_decompose(
            sectors=["A", "B"],
            port_weights=[0.5, 0.5],
            bench_weights=[0.5, 0.5],
            port_returns=[0.08, 0.04],
            bench_returns=[0.05, 0.01],
        )
        assert result.total_allocation == pytest.approx(0.0)
        assert result.total_selection > 0

    def test_active_return_equals_sum(self):
        """Active return = allocation + selection + interaction."""
        result = brinson_decompose(
            sectors=["A", "B", "C"],
            port_weights=[0.5, 0.3, 0.2],
            bench_weights=[0.3, 0.4, 0.3],
            port_returns=[0.03, 0.02, 0.01],
            bench_returns=[0.02, 0.02, 0.005],
        )
        total = result.total_allocation + result.total_selection + result.total_interaction
        assert total == pytest.approx(result.active_return)

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            brinson_decompose(
                sectors=["A", "B"],
                port_weights=[0.5, 0.5],
                bench_weights=[0.4, 0.4, 0.2],  # wrong length
                port_returns=[0.01, 0.01],
                bench_returns=[0.01, 0.01, 0.0],
            )

    def test_negative_returns(self):
        result = brinson_decompose(
            sectors=["A", "B"],
            port_weights=[0.5, 0.5],
            bench_weights=[0.5, 0.5],
            port_returns=[-0.03, -0.04],
            bench_returns=[-0.05, -0.01],
        )
        assert result.total_selection < 0  # worse picks in A, B
        assert isinstance(result.active_return, float)

    def test_to_dict(self):
        result = brinson_decompose(
            sectors=["A"],
            port_weights=[1.0],
            bench_weights=[1.0],
            port_returns=[0.02],
            bench_returns=[0.01],
        )
        d = result.to_dict()
        assert d["sectors"] == ["A"]
        assert "total_allocation" in d
        assert "total_selection" in d


class TestBrinsonMultiPeriod:
    def test_two_periods(self):
        result = brinson_multi_period(
            sectors=["A", "B"],
            period_port_weights=np.array([[0.7, 0.3], [0.6, 0.4]]),
            period_bench_weights=np.array([[0.5, 0.5], [0.5, 0.5]]),
            period_port_returns=np.array([[0.02, 0.01], [0.01, 0.03]]),
            period_bench_returns=np.array([[0.015, 0.01], [0.01, 0.02]]),
        )
        assert len(result.periods) == 2
        assert isinstance(result.cumulative_active_return, float)
        assert isinstance(result.avg_allocation_effect, float)

    def test_geometric_linking(self):
        result = brinson_multi_period(
            sectors=["A"],
            period_port_weights=np.array([[1.0], [1.0]]),
            period_bench_weights=np.array([[1.0], [1.0]]),
            period_port_returns=np.array([[0.10], [-0.05]]),
            period_bench_returns=np.array([[0.05], [-0.02]]),
            linking_method="geometric",
        )
        # Geometric: (1.05)×(0.97) - 1 ≈ 0.0185
        assert result.cumulative_active_return != 0.0

    def test_to_dict(self):
        result = brinson_multi_period(
            sectors=["A"],
            period_port_weights=np.array([[1.0]]),
            period_bench_weights=np.array([[1.0]]),
            period_port_returns=np.array([[0.02]]),
            period_bench_returns=np.array([[0.01]]),
        )
        d = result.to_dict()
        assert d["period_count"] == 1
        assert "cumulative_active_return" in d
