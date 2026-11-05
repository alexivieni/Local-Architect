import argparse
import sys

from .engine import Game
from .render import render

KEYS = {"w": "up", "a": "left", "s": "down", "d": "right"}


def main(argv=None, read=input, write=print):
    ap = argparse.ArgumentParser(prog="__PKG__", description="__NAME__: 2048 in the terminal")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args(argv)
    game = Game(args.seed)
    while True:
        write(render(game.board, game.score))
        if game.over:
            write("game over")
            return 0
        try:
            key = read("move (w/a/s/d, q quits): ").strip().lower()
        except EOFError:
            return 0
        if key == "q":
            return 0
        if key in KEYS:
            game.play(KEYS[key])


if __name__ == "__main__":
    sys.exit(main())
