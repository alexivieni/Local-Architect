"""The board as text: fixed-width cells so columns line up."""


def render(board, score=0):
    width = max(4, max(len(str(v)) for row in board for v in row))
    line = "+" + "+".join("-" * (width + 2) for _ in board[0]) + "+"
    out = [f"score: {score}", line]
    for row in board:
        out.append("|" + "|".join(f" {(str(v) if v else '.'):>{width}} " for v in row) + "|")
        out.append(line)
    return "\n".join(out)
