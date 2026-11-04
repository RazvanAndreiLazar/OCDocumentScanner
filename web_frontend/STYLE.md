# Style guide

How the web front end looks, and how to change it. Everything is in one file, [`static/app.css`](static/app.css). There is no CSS framework.

**The idea:** a flatbed scanner. The header is the body of the machine, with the ruler of the glass along its edge. Every page is one sheet of paper slid against that ruler. The lamp is the one bold element: it passes once when the first page opens, and on the scan page it shows how far the scan is.

## Colours

There are four, on top of the paper they lie on, and each colour scheme has its own values. To re-colour the interface, change the two blocks at the top of `app.css` and nothing else:

| Token | Light | Dark | Used for |
|---|---|---|---|
| `--color-primary` | `#0a7a52` | `#45c595` | The main action of a page, "available", links, focus ring, the lamp. Green, like the Start key of a copier |
| `--color-secondary` | `#2f5d86` | `#84b4de` | The second action, "in use", the strip that shows a running scan |
| `--color-ink` | `#1b2a36` | `#e4eaef` | Text. All greys are this colour mixed with the paper, so they follow it |
| `--color-alert` | `#b5341c` | `#f2846e` | Errors, "not reachable", deleting |
| `--paper` | `white` | `#1a242d` | What content lies on, and the text on a filled colour |

**When you change them, check the contrast** (any WCAG contrast checker). Against `--paper`, in the same colour scheme:

- `--color-ink`, `--color-primary` and `--color-alert` are used as text on the paper: at least 4.5:1.
- `--color-primary`, `--color-secondary` and `--color-alert` have text in the paper colour on them: the same 4.5:1. That is why the dark colours are light, and a filled button has dark text there.

The scheme is `data-theme` on `<html>`, which the server writes from the saved setting, so a page never flashes the other colours. The button at the top right saves it with `POST /settings/theme`.

Components don't use anything but these tokens and the ones derived from them. `tests/test_web_routes.py` fails if a colour value appears anywhere else in the stylesheet.

### Derived colours

Section 2 of `app.css` makes these from the palette. Use them instead of mixing a new shade:

| Token | Used for |
|---|---|
| `--glass` | The page behind the paper |
| `--frame`, `--on-frame` | The header, and what is on it |
| `--on-color` | Text on a filled colour |
| `--muted` | Less important text |
| `--edge` | The border of a control |
| `--rule` | Lines that only separate |
| `--hover` | A row or quiet button under the pointer |
| `--primary-pressed`, `--secondary-pressed` | A filled button under the pointer |
| `--lamp` | The lamp and the corner mark |
| `--shadow` | Under what lies on top of the page, like the open dropdown |

`--glass`, `--frame`, `--shadow` and `--pressed-toward` can't be worked out the same way in both schemes (the page behind the paper is darker than the paper in both), so section 2 has a small block for dark.

Firefox doesn't paint a repeating gradient from a derived colour to `transparent`. In that case use a palette colour with `opacity`, like `.sheet-lines` does.

## Type

[Archivo](https://github.com/Omnibus-Type/Archivo) (variable weight), in `static/vendor/archivo/` under the SIL Open Font License. It is served by the app itself, so the interface looks the same on a network without internet.

| Token | Size at 15px | Used for |
|---|---|---|
| `--text-title` | 24px, bold | The one `h1` of a page |
| `--text-heading` | 18px | `h2`, the wordmark |
| `--text-body` | 15px | Everything else |
| `--text-small` | 13px | Field labels, status, secondary text |

The root size is 15px on a desktop and 16px on a touch screen, set in one place (`html` in section 3).

Text is in sentence case. A button says what it does ("Start scan"), and an error says what to do about it.

## Size and shape

- **Spacing:** `--space-1` to `--space-8`, from 4px to 30px at 15px text. Don't write other distances.
- **Controls:** `--control-height` is 36px with a mouse and 44px on a touch screen (`@media (pointer: coarse)`). Set the size of a new control with it.
- **Corners:** `--radius` (3px) for controls. The paper stays square, it is a sheet.
- **Width:** `--paper-width` (30rem) for every page. Pages have one column, with fields side by side in a `.field-row`.

## Components

| Class | What it is |
|---|---|
| `.button` + `--primary` | The main action. At most one on a screen |
| `.button` + `--secondary` | The second action |
| `.button` + `--quiet` | An action that matters less, such as Cancel |
| `.button` + `--danger` | Red text only, for deleting |
| `.field`, `.field-label`, `.field-row` | A labelled input or select, and fields side by side |
| `.field-status` | The result of a field that saves itself. Its height is reserved so that nothing jumps under the pointer |
| `.check` | A checkbox or radio with its text |
| `.message` | An error, from `_message.html` |
| `.status` + `data-state` | The availability of a printer: a dot and a word, so it doesn't depend on seeing the colour |
| `.picker` | The printer dropdown, see below |
| `.rows`, `.row` | A list where tapping a row is the action |
| `.spinner` | Waiting for something slow, with text that says what |
| `.paper`, `.frame`, `.ruler` | The page itself, from `base.html` |
| `.theme-toggle` | The button at the top right of the header, from `settings/_theme_toggle.html` |
| `.bed`, `.sheet`, `.sheet-lamp` | The scan on the glass, from `scan/job.html` |

**The picker** isn't a `<select>`, because an `<option>` can't show a coloured status. It is one list of buttons, with the active printer always first; closed, only the first row is visible. `static/app.js` opens and closes it (Escape and the arrow keys work too).

## Motion

Only what means something moves: the lamp, and the spinner. No fades or hover animations. All of it is reduced or off under `prefers-reduced-motion`.
