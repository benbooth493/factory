# Problem type → impeccable command

The checker gives each problem one `type`. This table is the default command
for it; the fixer may change it when the code shows a better fit and records
what it actually ran.

| `type` | Looks like | Command |
|---|---|---|
| `cluttered` | too much on screen, duplicate actions, chrome competing with the task | `distill` |
| `bland` | generic, forgettable, and the brief asks for more presence | `bolder` |
| `too-loud` | too many colours, weights, borders, shadows or badges | `quieter` |
| `dull-color` | flat grey where colour should carry meaning | `colorize` |
| `spacing-hierarchy` | cramped or loose spacing, weak grouping, misalignment | `layout` |
| `typography` | type scale, weights, line length, figures, truncation | `typeset` |
| `responsive` | breaks at 390, sideways scroll, small tap targets | `adapt` |
| `performance` | layout shift, heavy images, slow render | `optimize` |
| `first-run-empty` | an empty or first-run state that doesn't point to the next step | `onboard` |
| `confusing-copy` | labels, errors or status words that confuse | `clarify` |
| `edge-cases` | long content, error and loading states, overflow, i18n | `harden` |
| `accessibility` | contrast, focus, names, keyboard, heading order | `harden` |
| `needs-motion` | state changes with no feedback, and the brief wants motion | `animate` |
| `personality` | correct but lifeless, and the brief wants character | `delight` |
| `drift` | the same pattern copied 3+ times, raw values beside tokens | `extract` |
| `polish` | small misalignments and inconsistencies | `polish` |

## Chaining

1. Order by the severity of the problems each command fixes. On a tie,
   structure before surface: `distill` / `extract` → `layout` / `adapt` →
   `typeset` / `colorize` / `quieter` / `bolder` → `clarify` / `harden` /
   `onboard` → `animate` / `delight` / `optimize`.
2. **`polish` always runs last**, once, over everything the round touched.
3. Two commands pulling against each other on the same element (`bolder`
   and `quieter`): keep the higher-severity one, skip the other with a reason.
4. `bolder`, `colorize`, `animate` and `delight` add things. Run them only
   when the brief or the taste file's direction asks for more.
