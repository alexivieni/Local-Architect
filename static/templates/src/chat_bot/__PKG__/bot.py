"""The bot: a router of `!name args` commands. Time and randomness are injected so tests are exact."""
import random
import shlex
import time
from dataclasses import dataclass


@dataclass
class Message:
    user: str
    channel: str
    text: str


class Bot:
    def __init__(self, prefix="!", cooldown_s=2.0, clock=time.monotonic, rng=None):
        self.prefix, self.cooldown_s, self.clock = prefix, cooldown_s, clock
        self.rng = rng or random.Random()
        self.commands = {}
        self.last_used = {}
        self.reminders = []          # (due, channel, user, text)

    def command(self, name, help=""):
        def deco(fn):
            self.commands[name] = (fn, help)
            return fn
        return deco

    def handle(self, msg):
        """-> reply text, or None when the message is not for the bot."""
        if not msg.text.startswith(self.prefix):
            return None
        try:
            parts = shlex.split(msg.text[len(self.prefix):])
        except ValueError:
            return "I could not read that (unbalanced quotes?)."
        if not parts:
            return None
        name, args = parts[0].lower(), parts[1:]
        if name not in self.commands:
            return f"Unknown command `{name}`. Try {self.prefix}help."
        now = self.clock()
        key = (msg.user, name)
        if name != "help" and now - self.last_used.get(key, -1e9) < self.cooldown_s:
            return "Slow down a little."
        self.last_used[key] = now
        try:
            return self.commands[name][0](msg, args)
        except (ValueError, IndexError) as e:
            return f"Usage problem: {e}"

    def due(self):
        """Pop reminders whose time has come. -> list of (channel, text)."""
        now = self.clock()
        ready = [r for r in self.reminders if r[0] <= now]
        self.reminders = [r for r in self.reminders if r[0] > now]
        return [(ch, f"<@{user}> reminder: {text}") for _, ch, user, text in sorted(ready)]


def build(**kw):
    bot = Bot(**kw)

    @bot.command("help", help="list commands")
    def _help(msg, args):
        return "\n".join(f"{bot.prefix}{n} - {h}" for n, (_, h) in sorted(bot.commands.items()))

    @bot.command("roll", help="roll dice, e.g. 2d6")
    def _roll(msg, args):
        spec = (args or ["1d6"])[0].lower()
        n, _, sides = spec.partition("d")
        n, sides = int(n or 1), int(sides)
        if not (1 <= n <= 20 and 2 <= sides <= 1000):
            raise ValueError("between 1 and 20 dice, 2 to 1000 sides")
        rolls = [bot.rng.randint(1, sides) for _ in range(n)]
        return f"{spec}: {rolls} = {sum(rolls)}"

    @bot.command("remind", help="remind MINUTES text")
    def _remind(msg, args):
        minutes = float(args[0])
        if not 0 < minutes <= 24 * 60 or len(args) < 2:
            raise ValueError("remind MINUTES text (up to a day)")
        bot.reminders.append((bot.clock() + minutes * 60, msg.channel, msg.user, " ".join(args[1:])))
        return f"OK, in {minutes:g} min."

    return bot
