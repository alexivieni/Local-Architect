# __NAME__

2048 in the terminal. The rules live in `__PKG__/engine.py` as pure functions over a 4x4 grid (a seeded
`random.Random` decides where tiles appear, so games are repeatable); `__PKG__/__main__.py` is only the loop.

    python -m __PKG__          # w/a/s/d to slide, q to quit
    python -m __PKG__ --seed 7 # the same game every time
    python -m pytest -q

## Layout

    __PKG__/engine.py    slide/merge rules, spawning, win and game-over detection
    __PKG__/render.py    the board as text
    __PKG__/__main__.py  the input loop
    tests/               engine and render tests
