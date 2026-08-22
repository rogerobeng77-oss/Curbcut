PATCH_INSTRUCTION = """\
You are fixing one accessibility violation in a real source file.

Rule violated: {rule}
Impact: {impact}
Rule description: {description}
CSS selector on the rendered page: {selector}
Rendered element markup: {html}

Source file: {path}, line {line}
Exact source line:
{source_line}

You are also given a full-page screenshot of the rendered page. Use it to
understand what the element is and what it does before choosing a fix.

Return ONLY a JSON object with exactly these keys:
  "old"       - the source line to replace, character-for-character identical
                to the exact source line above
  "new"       - the replacement line
  "rationale" - one sentence explaining the fix in plain language

Rules:
- Change only what is needed to resolve this violation.
- Preserve the existing indentation exactly.
- For alt text, describe what the image conveys; never write "image" or the filename.
- For contrast, choose the nearest colour that reaches at least 4.5:1 against the
  background while staying close to the original hue.
- Do not wrap the JSON in markdown fences or add commentary.
"""
