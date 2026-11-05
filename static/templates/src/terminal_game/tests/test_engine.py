import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from __PKG__ import engine  # noqa: E402


def test_slide_row_merges_once_per_tile():
    assert engine.slide_row((2, 2, 2, 2)) == ((4, 4, 0, 0), 8)
    assert engine.slide_row((2, 0, 2, 4)) == ((4, 4, 0, 0), 4)
    assert engine.slide_row((4, 4, 8, 0)) == ((8, 8, 0, 0), 8)
    assert engine.slide_row((0, 0, 0, 2)) == ((2, 0, 0, 0), 0)


def test_move_in_every_direction():
    b = ((2, 0, 0, 2), (0, 0, 0, 0), (0, 0, 0, 0), (2, 0, 0, 0))
    assert engine.move(b, "left")[0][0] == (4, 0, 0, 0)
    assert engine.move(b, "right")[0][0] == (0, 0, 0, 4)
    up, pts = engine.move(b, "up")
    assert up[0] == (4, 0, 0, 2) and pts == 4
    assert engine.move(b, "down")[0][3] == (4, 0, 0, 2)
    with pytest.raises(ValueError):
        engine.move(b, "sideways")


def test_spawn_fills_one_empty_cell_and_is_seeded():
    b1 = engine.spawn(engine.empty(), random.Random(1))
    b2 = engine.spawn(engine.empty(), random.Random(1))
    assert b1 == b2 and sum(v != 0 for row in b1 for v in row) == 1


def test_stuck_board_is_over_and_win_is_detected():
    stuck = ((2, 4, 2, 4), (4, 2, 4, 2), (2, 4, 2, 4), (4, 2, 4, 2))
    assert not engine.can_move(stuck)
    assert engine.won(((2048, 0, 0, 0),) + ((0,) * 4,) * 3)


def test_game_counts_only_real_moves():
    g = engine.Game(seed=3)
    start = g.board
    changed = [g.play(d) for d in engine.DIRECTIONS]
    assert any(changed) and g.moves == sum(changed) and g.board != start
