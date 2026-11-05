import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from __PKG__.bot import Message, build  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make():
    clock = Clock()
    return build(clock=clock, rng=random.Random(4)), clock


def say(bot, text, user="u1"):
    return bot.handle(Message(user, "c1", text))


def test_non_commands_are_ignored_and_unknown_is_explained():
    bot, _ = make()
    assert say(bot, "hello there") is None
    assert "Unknown command" in say(bot, "!dance")


def test_help_lists_every_command():
    bot, _ = make()
    out = say(bot, "!help")
    assert all(f"!{n}" in out for n in ("help", "roll", "remind"))


def test_roll_is_seeded_and_validated():
    bot, clock = make()
    out = say(bot, "!roll 3d6")
    assert out.startswith("3d6: [") and 3 <= int(out.rsplit("= ", 1)[1]) <= 18
    clock.t += 10
    assert "Usage problem" in say(bot, "!roll 99d6")


def test_cooldown_is_per_user_and_per_command():
    bot, clock = make()
    say(bot, "!roll")
    assert say(bot, "!roll") == "Slow down a little."
    assert say(bot, "!roll", user="u2") != "Slow down a little."
    clock.t += 5
    assert say(bot, "!roll") != "Slow down a little."


def test_reminders_fire_once_when_due():
    bot, clock = make()
    assert say(bot, "!remind 1 stand up") == "OK, in 1 min."
    assert bot.due() == []
    clock.t += 61
    assert bot.due() == [("c1", "<@u1> reminder: stand up")] and bot.due() == []


def test_bad_quotes_do_not_crash():
    bot, _ = make()
    assert "could not read" in say(bot, '!remind 1 "oops')
