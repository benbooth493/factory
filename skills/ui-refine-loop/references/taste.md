# Taste and direction

<!-- Written by /factory:ui-refine-loop from this template (default path
.claude/factory/taste.md, set by ui.taste_file). One file per project; each
refined target gets its own block under Direction. Checkers and fixers read
it before anything else and cite rules by id. Edit the rules to match the
project. -->

## Direction

### <target>

- **Goal:** <what someone should be able to do or see>
- **Direction:** <calmer | bolder | denser | more spacious | same look, cleaner>
- **Keep:** <what must not change>
- **Avoid / off-limits:** <files, components, copy the fixer must not touch>
- **Users:** <who, how often>
- **Reference:** <a product or page whose feel is right, if any>
- **Colour / type / motion / dark mode / mobile:** <answers, if asked>

## Rules

| id | Rule |
|---|---|
| T1 | Stay in the existing visual world: tokens, fonts, radii and shared components stay unless Direction says otherwise. |
| T2 | The primary task is what the eye lands on first. |
| T3 | One colour per meaning, from tokens. Colour marks meaning, not decoration. |
| T4 | One spacing scale and one type scale. Related things sit closer than unrelated ones. |
| T5 | Content reads along a left edge; labels, figures and actions line up. |
| T6 | Dark mode has the same hierarchy and contrast as light. |
| T7 | At 390 wide nothing clips, scrolls sideways, or has a tap target under 24px. |
| T8 | Cut before adding: remove duplicates and decoration before adding sections, colour or motion. |
| T9 | Copy says what things do; errors say what failed and how to fix it. |
| T10 | Reuse shared components and extend them by prop; a one-off copy is a problem even if it looks good. |

## Slop tells

Each hit in a screenshot is a problem citing the tell and the rule.

- **S1** A grid of identical tiles: icon, bold title, two lines of filler. (T2, T8)
- **S2** Gradient text, glows, shimmer or pulse used as decoration. (T3)
- **S3** Emoji as icons, or an icon-in-a-box above every heading. (T8)
- **S4** Tinted banners and badges on everything. (T3, T8)
- **S5** Everything centred, nothing to read along. (T5)
- **S6** Decorative backgrounds competing with the task. (T2)
- **S7** Made-up stats or claims the product can't back. (T9)
- **S8** Marketing-size padding and type on a working app screen. (T4)
- **S9** Two headers, a card inside a card, two buttons doing the same thing. (T8)
- **S10** Dark mode as inverted light: pure black, grey-on-grey text. (T6)
