# Printer backend

Finds scanners on the network, reads what each one supports, and runs scans through the command-line tools `scanimage` (SANE) and `img2pdf`.

The backend does no direct user input or output during a scan. It reports what happens through events and receives answers from the caller, so a CLI and a GUI can both use it. `main_cli.py` is the current front end.

| File | Contents |
|---|---|
| `printer.py` | `Printer`: discovery, per-source options, scan methods |
| `options.py` | `ScanOption` and `parse_help`, the parser for `scanimage` option listings. It runs no commands |
| `scanning.py` | Constants, scan events, `source_kind`, `run_command`, and the version check `init` |

`printer.py` imports from the other two files, and they don't import it.

**Requirements:** `scanimage` from `sane-utils`, and `img2pdf` for PDF output. The code was written against sane-backends 1.2.1.

## Using a printer

```python
lines = await Printer.discover_printers()                  # raw `scanimage -L` lines
printer = Printer(Printer.printer_name_from_discovery_line(lines[0]))
await printer.load_options()

printer.select_source('ADF')
printer.set_option('resolution', 300)                      # ValueError if not allowed

scan = printer.scan(two_sided=True)                        # see "Events" for how to run it
```

## Options

`load_options` runs `scanimage -d DEVICE --source X -A` once per source. It stores the results in `printer.options` as `{source: {name: ScanOption}}`. A device without a `source` option gets one entry under `NO_SOURCE` (`''`).

**Why options are stored per source:** a source can change what other options allow. On the ENVY 6400, the flatbed offers up to 1200 dpi and the feeder only up to 300 dpi.

**Why `--source` is always passed:** hpaio lists only up to 300 dpi until a source is given explicitly, even though its default listing already shows `[Flatbed]`.

**Why `-A` and not `--help`:** `--help` also lists every device on the network. That took about 12 s per call, against about 0.3 s for `-A`.

A `ScanOption` is one of these types:

| Type | Listing example | Fields used |
|---|---|---|
| Enumeration | `--mode Lineart\|Gray\|Color [Color]` | `choices` |
| Range | `-x 0..215.9mm [215.9]` | `min`, `max`, `unit` |
| Yes/no | `--negative[=(yes\|no)] [no]` | `choices`, `is_boolean` (sent as `--negative=yes`) |
| Free value | `--adf-justification-x <string> [inactive]` | none, so any value is accepted |

The bracketed value after the allowed values is stored in both `value` and `default`. Tags such as `[inactive]` and `[advanced]` are not values. `[inactive]` sets `active = False`, and `validate` then refuses the option.

`set_option(name, value)` validates against the current source and then updates `value`. Each source keeps its own values.

`to_args()` builds the arguments for a scan: `--source`, plus the active options whose `value` differs from `default`. Unchanged options are deliberately not sent. hpaio lists `-y` as `297.011` but rejects that exact value when it is sent back.

## Scanning

### Choosing a method

`printer.scan(...)` chooses the method from `source_kind(printer.source)`. `source_kind` matches the source name against `SCAN_KNOWN_KEYWORDS`.

| Kind | `scan()` uses | `scan_one_at_a_time` | `scan_all` |
|---|---|---|---|
| `flatbed` | `scan_one_at_a_time` | allowed | `ValueError` |
| `document-feeder` | `scan_all` | `ValueError` | allowed |
| `duplex` | `scan_all` | `ValueError` | allowed |
| unknown or no source | must pass `one_at_a_time=True/False`, otherwise `ValueError` | allowed | allowed |

Passing `one_at_a_time` on a known source doesn't override the source. A conflicting value is rejected. Arguments and source type are checked when the method is called, before the scans folder is touched.

### What each scan does

Every scan starts with `clean_scans_folder()`, which deletes everything in `./scans`. Nothing is cleaned afterwards.

- **One at a time:** one `scanimage -o` call per page. It asks `NEXT_PAGE` before every page after the first, and stops at `count` pages or when the answer isn't true.
- **Feeder, one side:** one `scanimage --batch` run.
- **Feeder, two sides:**
  1. The fronts are scanned with odd numbers (`scan001`, `scan003`, ...).
  2. It asks `FLIP`.
  3. The backs are scanned as `back001`, `back002`, ...
  4. The backs are renamed in reverse order. With 3 sheets, `back001` becomes `scan006`, `back002` becomes `scan004` and `back003` becomes `scan002`, because the flipped stack feeds the last sheet first.
  5. If the number of backs and fronts differ, the scan stops with an error and keeps the pages.
- **Duplex, combined PDF:** `scanimage --format pdf --batch=test.pdf` writes the whole document. In other cases it runs like a one-sided feeder.
- **Unknown source through `scan_all`:** emits a warning, then runs like a one-sided feeder.

### Output

`output_format` is one of `tiff`, `pnm`, `png`, `jpeg`, `pdf`.

- **Other formats:** pages are written directly in that format, and `combine=True` raises `ValueError`.
- **PDF:** pages are scanned as PNG, because `--format pdf` puts a whole batch into one file. `img2pdf` then builds `test.pdf` when `combine` is set, or one PDF per page otherwise. The exception is the duplex case above.

`combine` defaults to `False` for one-at-a-time scans and `True` for `scan_all`.

### Events

Scan methods return async generators. The event classes are in `scanning.py`.

| Event | Meaning | Answer to send |
|---|---|---|
| `Progress(text)` | Output from `scanimage` or `img2pdf`, or a warning | none |
| `PageScanned(path, number)` | A page was written | none |
| `NeedsInput(NEXT_PAGE)` | Scan another page? | `True` continues; anything else stops |
| `NeedsInput(FLIP)` | Turn the stack around | anything, once it's done |
| `Done(files)` | The finished files | none |

```python
async with aclosing(printer.scan()) as events:
    try:
        event = await anext(events)
        while True:
            reply = None
            ...  # show the event; for NeedsInput, ask the user and set reply
            event = await events.asend(reply)
    except StopAsyncIteration:
        pass
```

Failures raise `RuntimeError` from the generator. Closing the generator stops any running command. `main_cli.py` has a complete example in `run_scan`.

### `scanimage` behavior the code works around

- **Negative batch increment:** with `--batch-increment` below zero, `scanimage` exits with status 7 when the feeder runs out, however many pages it scanned. Its end-of-batch check assumes numbers go up (`n > batch_start_at` in `frontend/scanimage.c`). That is why backs are renamed afterwards.
- **Failure to open airscan:** airscan sometimes fails with `open of device ... failed: Invalid argument`. `_scanimage` retries that error (`OPEN_ATTEMPTS`, `OPEN_RETRY_DELAY`), because nothing has been scanned at that point.
- **Broken `escl` output:** the `escl` backend prints gzip data in `scanimage -L`. `discover_printers` drops lines with non-printable characters.

## Shortcomings

### Source names are matched against a short keyword list

`SCAN_KNOWN_KEYWORDS` in `scanning.py` contains only `Flatbed`, `ADF`, `Automatic Document Feeder` and `Duplex`. Only `Flatbed` and `ADF` have been seen on a real device. Source names vary by backend, so a real flatbed or feeder on another printer may be treated as unknown.

These names are all currently unknown: `Platen`, `Document Table`, `Document Feeder`, `Automatic Feeder`, `Simplex`, `Normal`, `Auto`, `Transparency Adapter`. They are examples to check, not names confirmed from real devices.

An unknown source has real consequences:
- `scan()` refuses to run without `one_at_a_time`, and the CLI asks the user to choose.
- The source guard is skipped, so either method is allowed.
- A flatbed run through `scan_all` is treated as a feeder. `scanimage --batch` may then keep rescanning, since a flatbed never runs out of paper. This hasn't been tested.
- `two_sided` is ignored, because the two-sided flow only runs for `document-feeder`.

To add a source:
1. Add its keyword to the right group in `SCAN_KNOWN_KEYWORDS`. Matching is a case-insensitive substring test, checked in the order duplex, flatbed, feeder.
2. Add a row to `SourceKindTest` in `tests/test_scanning.py`.
3. Add a saved `-A` listing to `tests/fixtures/`, ideally.

Substring matching can also misclassify a source: any name containing `adf` counts as a feeder.

### What has been tested on real hardware

Everything was tried on one printer, the HP ENVY 6400, through the `hpaio` and `airscan` backends.

| Feature | Status |
|---|---|
| Discovery and loading options | Works on both backends |
| Two-sided feeder scanning | Every page was scanned in a real run, but the merge failed because of the negative increment problem |
| Renaming the backs (the fix for that) | Only tested with a stand-in `scanimage` |
| Retry when the device doesn't open | Only tested with fakes. The cause of the error is unknown, and it didn't reproduce on an idle printer |
| Flatbed through the current code | Not confirmed on the printer |
| Duplex | Never run. This printer has no duplex source, and the single-PDF output is assumed |
| Page order after flipping | Reverse order was confirmed for this printer. Other feeders may differ |

### Options

- **Passing validation doesn't guarantee acceptance:** `validate` only checks the listed values. The device can still reject a value inside the range, as hpaio does with `-y 297.011`. Only unchanged options are protected from this.
- **Only the source triggers a re-read:** a sweep of single-option changes on both backends found no other dependencies. Combinations of options and other printers weren't tested. Options that are `[inactive]` stay inactive for the whole session, even if another setting would enable them.
- **The parser depends on `scanimage`'s text layout:** it expects specific indentation and formatting. Output from other versions may parse incorrectly, and a wrapped option line would be missed.
- **Options without a value are mishandled:** a listing like `--calibrate` parses as a free value that accepts anything, so `to_args` would send it with a value.
- **Display bugs in `ScanOption.__str__`:**
  - The current value is found by substring, so `200` in a list starting with `1200` displays as `1[200]`.
  - An active enumeration with no value raises `TypeError`.
  - The yes/no branch is never reached, because those options have `choices`.
- **Default resolution:** nothing sets a sensible default. hpaio's listed default of 75 dpi is low for OCR.

### Scanning and files

- **Results are temporary:** the next scan deletes everything in `./scans`, including finished documents and anything else in that folder. Files need to be copied out first. The user is warned, but nothing asks for confirmation.
- **Fixed paths:** the document is always named `test.pdf`, from `DEFAULT_FILE`, and `SCAN_FOLDER_DEFAULT` is relative to the working directory. Neither can be set per scan, and two `Printer` objects would share the folder.
- **No resume:** an error during a two-sided scan leaves the fronts on disk, but there's no way to continue, and the next scan deletes them.
- **Rough error messages:** an empty feeder surfaces as `scanimage failed (7)` with raw output. The retry depends on matching the text `open of device`, which could change or be translated.
- **Limited retry:** `load_options` doesn't retry the open failure, although it has been seen there too.
- **Leftover pages:** page images stay beside the finished PDF until the next scan.

### Structure and integration

- **`init()` isn't called:** nothing runs the version check, and it `print`s warnings directly, unlike the rest of the backend. It also needs an exact version match. On the development machine, `scanimage` reports 1.2.1 while the `sane-utils` package is 1.3.1.
- **Discovery:**
  - `discover_printers` takes about 12 s and has no cache or saved printers.
  - Its noise filter can miss garbage that happens to be printable.
  - It opens a stderr pipe it never reads.
  - A missing `scanimage` raises `FileNotFoundError`. `init` and `run_command` raise a clearer `RuntimeError`.
- **CLI error handling:** `run()` doesn't catch failures from discovery or `load_options`, so they end in a traceback.
- **The CLI still knows about source kinds:** it calls `source_kind` to decide what to ask. A capability query on `Printer` would remove that dependency.
- **`Printer` does a lot:** it holds device state, options, discovery and the scan flows. `_scan` in particular handles every mode in one function.
- **No OCR step:** output PDFs contain images only.

## Tests

```
python3 -m unittest discover -s tests
```

The tests need no printer. They parse saved listings in `tests/fixtures/` and replace `scanimage` and `img2pdf` with a fake that creates files and copies the batch exit behavior. So they check the code's logic, not real device behavior.
