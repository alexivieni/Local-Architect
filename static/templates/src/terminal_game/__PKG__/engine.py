"""2048 rules as pure functions. A board is a tuple of 4 tuples of ints (0 = empty)."""
import random

SIZE = 4
WIN = 2048
DIRECTIONS = ("left", "right", "up", "down")


def empty():
    return tuple((0,) * SIZE for _ in range(SIZE))


def slide_row(row):
    """Slide one row to the left. -> (new row, points scored). Each tile merges at most once per move."""
    tiles = [v for v in row if v]
    out, score, i = [], 0, 0
    while i < len(tiles):
        if i + 1 < len(tiles) and tiles[i] == tiles[i + 1]:
            out.append(tiles[i] * 2)
            score += tiles[i] * 2
            i += 2
        else:
            out.append(tiles[i])
            i += 1
    return tuple(out + [0] * (len(row) - len(out))), score


def _transpose(board):
    return tuple(zip(*board))


def move(board, direction):
    """-> (new board, points). The board is unchanged when the move does nothing."""
    if direction not in DIRECTIONS:
        raise ValueError(f"unknown direction {direction!r}")
    b = _transpose(board) if direction in ("up", "down") else board
    rows, score = [], 0
    for row in b:
        r = row[::-1] if direction in ("right", "down") else row
        r, s = slide_row(r)
        rows.append(r[::-1] if direction in ("right", "down") else r)
        score += s
    out = tuple(rows)
    return (_transpose(out) if direction in ("up", "down") else out), score


def spawn(board, rng):
    """Put a 2 (90%) or a 4 (10%) on a random empty cell. A full board is returned unchanged."""
    cells = [(r, c) for r in range(SIZE) for c in range(SIZE) if board[r][c] == 0]
    if not cells:
        return board
    r, c = rng.choice(cells)
    value = 4 if rng.random() < 0.1 else 2
    grid = [list(row) for row in board]
    grid[r][c] = value
    return tuple(tuple(row) for row in grid)


def can_move(board):
    return any(move(board, d)[0] != board for d in DIRECTIONS)


def won(board):
    return any(v >= WIN for row in board for v in row)


class Game:
    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.board = spawn(spawn(empty(), self.rng), self.rng)
        self.score = 0
        self.moves = 0

    def play(self, direction):
        """-> True if the move changed the board (a new tile then appears)."""
        new, pts = move(self.board, direction)
        if new == self.board:
            return False
        self.board = spawn(new, self.rng)
        self.score += pts
        self.moves += 1
        return True

    @property
    def over(self):
        return not can_move(self.board)
