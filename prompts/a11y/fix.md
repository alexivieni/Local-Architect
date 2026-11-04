Fix what stops keyboard and screen-reader users from using these pages. Each finding names the file and line.
- an image with no alt -> add alt text that says what it shows, or alt="" if it is decoration;
- a field with no label -> add a <label for=...> (or wrap it), or aria-label if there is no room for visible text;
- a button or link with no text -> give it visible text or an aria-label;
- a clickable <div>/<span> -> make it a <button type="button">;
- <html> with no lang -> add lang="en" (or the page's language); a page with no <title> -> add one;
- a skipped heading level -> use the next level down;
- a positive tabindex -> remove it.
Do not redesign the page or change what it looks like beyond what the fix needs.

FINDINGS:
{findings}

PAGES:
{context}

{feedback}
