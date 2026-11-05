import sys

from .bot import Message, build


def main(argv=None, read=input, write=print):
    argv = sys.argv[1:] if argv is None else argv
    if "--console" not in argv:
        write("Run with --console to chat locally; see README for Slack/Discord.")
        return 0
    bot = build(cooldown_s=0)
    while True:
        try:
            line = read("you> ")
        except EOFError:
            return 0
        if line.strip() in ("quit", "exit"):
            return 0
        reply = bot.handle(Message("you", "console", line))
        for _, text in bot.due():
            write(text)
        if reply:
            write(reply)


if __name__ == "__main__":
    sys.exit(main())
