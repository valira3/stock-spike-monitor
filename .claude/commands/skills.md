---
description: List all project skills available in this repo
---

List the project skills available in `.claude/skills/` so the operator can pick one to invoke.

For each skill directory, print:
- The skill name (directory name)
- The `description` field from its `SKILL.md` YAML frontmatter
- A one-line summary of when to use it

Format as a markdown table:

```
| Skill | When to use |
|---|---|
| skill-name | Description from frontmatter, trimmed to one line |
```

Then suggest the operator: "To invoke a skill, ask me about the topic — I'll auto-load the SKILL.md if relevant."

Implementation:
1. `ls /home/user/stock-spike-monitor/.claude/skills/` → list of dirs
2. For each dir, read `SKILL.md` and parse the `description:` line from the YAML frontmatter
3. Print the table

If `.claude/skills/` is empty or missing, say so cleanly and suggest creating one.
