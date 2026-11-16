# RTM config: sterile code, private canonical data

Mongoose's RTM feature prints real documents (service-charge demands, receipts, letters) for a
specific company. To keep this code repository shareable, the real identity is NOT in the code. It
lives in a separate, strictly-private config repo and is imported at install time.

## Two repositories

| Repo | Holds | Sharing |
|------|-------|---------|
| **This code repo** | The engine, plus FICTIONAL placeholder identity (director "Brian Bennett", "Example Court", "Sample Bank", a fictional register). | Shareable. |
| **Private config repo** (`rtm-config`) | The REAL `rtm/property.json` (company block + register) and the REAL `.docx` letterhead. | Strictly private. |

## The seam

At runtime the loader prefers a file in the brain's `data_dir` over the packaged asset, for both the
property config and the Word templates:

- `rtm_example_court._property_file()`: `MONGOOSE_RTM_PROPERTY_FILE` -> `<data_dir>/rtm/property.json` -> packaged asset.
- `rtm_templates._docx_source()`: `<data_dir>/rtm_templates/docx/<file>` -> packaged asset.

`data_dir` is strictly `MONGOOSE_DB_PATH`'s parent (a brain-location key the settings layer refuses to
read from any `.env`). So the flats register AND the company identity travel together in one file,
bound to the brain path: point at the real brain and everything is real; point at a test brain and
everything is the packaged fictional set. They can never half-swap.

`company_profile()` FAILS LOUD if the `company` block is absent or incomplete: a real document must
never carry a placeholder director or sort code. There is no fictional fallback in source.

## Import (hydrate) a real install

```bash
scripts/rtm_hydrate.sh /path/to/rtm-config      # local checkout of the private repo
# or
scripts/rtm_hydrate.sh git@host:you/rtm-config.git
```

This copies the real `rtm/` and `rtm_templates/` into `data_dir` (backing up anything it overwrites)
and verifies the company block validates. Restart Mongoose so the running server reloads config.

## A fresh checkout with NO private config

Renders the fictional identity (Brian Bennett / Example Court / Sample Bank) everywhere. Safe by
construction: nothing real is ever produced without the private config present.
