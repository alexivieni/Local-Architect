Answer this question about the project by LOOKING, not by guessing. Work one step at a time: call a tool, read what it says, then decide the next step.

QUESTION: {goal}

TOOLS YOU MAY CALL:
{tools}

Rules:
- Answer with ONE JSON object per turn: {{"tool": "<name>", "args": {{...}}}} to look at something, or {{"final": "<your answer>"}} when you can answer.
- Start by finding out what exists (ls, search or grep) before you read a file.
- You have at most {max_steps} steps. Spend them on the parts that decide the answer.
- Your final answer names the files and functions it is based on (path:line), and says plainly what you could NOT establish. Never invent a name you have not seen.

{context}
