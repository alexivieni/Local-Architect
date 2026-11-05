import argparse
import sys

from .sim import Simulation


def main(argv=None):
    ap = argparse.ArgumentParser(prog="__PKG__", description="__NAME__: simulate a flat")
    ap.add_argument("--minutes", type=int, default=30)
    args = ap.parse_args(argv)
    sim = Simulation()
    sim.broker.subscribe("#", lambda t, p: print(f"{sim.minute // 60:02d}:{sim.minute % 60:02d} {t} {p}"))
    sim.run(args.minutes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
