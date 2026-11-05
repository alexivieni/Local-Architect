# __NAME__

A chat bot skeleton. All behaviour lives in `__PKG__/bot.py` (commands, cooldowns, reminders) and never touches
a network; `__PKG__/adapters.py` turns Slack / Discord payloads into `Message` objects and back.

    python -m __PKG__ --console     # talk to the bot in your terminal: !help, !roll 2d6, !remind 10 stretch
    python -m pytest -q

## Going live

- **Slack**: point an Events API / slash-command URL at a small HTTP handler that calls
  `adapters.verify_slack(secret, timestamp, body, signature)` and then `adapters.from_slack(payload)`.
  Keep the signing secret in the `SLACK_SIGNING_SECRET` environment variable, never in the code.
- **Discord**: `pip install discord.py`, then in `on_message` call `bot.handle(adapters.from_discord(msg))`.

## Adding a command

    @bot.command("echo", help="repeat what you said")
    def echo(msg, args):
        return " ".join(args) or "nothing to echo"
