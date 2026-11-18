"""explore: answer a question about a project by LOOKING (list, search, read), changing nothing. The judges are code: the final answer (read from the
run's trace) holds the facts the code really has (matched by regex so a paraphrase passes; a name the QUESTION already gives is not demanded back)
and none of the wrong ones, and no source file was touched. Tool use is not demanded: a first live run (2026-11-17, the 8B worker) answered
correctly from the code it was handed without a tool call, and a judge that fails a correct answer is wrong; `answer_mentions` can still ask for it (min_tools). The facts come from the bank's modules, so every answer is
checkable against the source; a model that guesses a plausible name it never saw fails on the needle it could not have known."""
import _bank as B

FLOW = "explore"
DEFAULT_N = 5

# module, question, regexes the answer must hold, regexes it must NOT hold, the first thing the scripted model looks at, the scripted answer
CASES = [
    ("cart", "Which method of the Cart class gives the total after a percentage discount, and which percentages does it accept?",
     [r"apply_discount", r"\b0\b.*\b100\b|0\s*(-|to|and)\s*100"], [r"\bsubtotal\(\)"], "apply_discount",
     "Cart.apply_discount(pct) in cart.py returns the discounted total; it accepts 0 to 100 and raises ValueError outside that range."),
    ("stats", "What does median() in stats.py return when the list has an even number of values?",
     [r"mean|average|two middle|middle two|both middle"], [r"\bmode\b"], "median",
     "median() in stats.py sorts the list and, for an even count, returns the mean of the two middle values; it raises ValueError for an empty list."),
    ("inventory", "What happens in remove_item() when the quantity left reaches zero or less, and what if the item is not there?",
     [r"delet|remov|disappear|drop", r"KeyError"], [r"returns? None"], "remove_item",
     "remove_item() in inventory.py deletes the item once nothing is left; if the item is not in the inventory it raises KeyError."),
    ("dates", "How does day_of_year() in dates.py work out its answer, and which helper does it rely on?",
     [r"days_in_month"], [r"calendar\.monthrange|strftime"], "days_in_month",
     "day_of_year() in dates.py adds up days_in_month() for every month before the given one and then adds the day; it does not use datetime."),
    ("textutil", "Which function in textutil.py shortens text, and what does it append when it cuts?",
     [r"truncate", r"ellipsis|…|\.\.\."], [r"slugify\(.*shorten"], "truncate",
     "truncate(text, n) in textutil.py cuts text to at most n characters and appends an ellipsis character when it had to cut."),
]


def generate(n, seed=0):
    rng = B.rng_for(seed, "explore")
    out = []
    for i, (mod, question, needles, forbid, look, answer) in enumerate(B.pick(rng, CASES, n)):
        tpath = B.test_path(mod)
        files = {f"{mod}.py": B.MODULES[mod]["src"], tpath: B.MODULES[mod]["tests"]}
        out.append({"id": f"explore-{mod}-{i:02d}", "tags": ["explore", "read-only"], "goal": question,
                    "fixture": {"files": files},
                    "budget": {"calls": 14, "tokens": 60000, "seconds": 600},
                    "judge": [{"type": "answer_mentions", "needles": needles, "forbid": forbid}, {"type": "files_unchanged", "paths": sorted(files)}],
                    "oracle": {"tool": "grep", "tool_args": {"pattern": look}, "answer": answer},
                    # a read-only flow has no file to write: the wrong answer is an edit, which files_unchanged catches (the answer judge is a trace judge)
                    "solution": {"files": {}}, "wrong": {"files": {f"{mod}.py": B.MODULES[mod]["src"] + "\n# edited by an explorer\n"}}})
    return out
