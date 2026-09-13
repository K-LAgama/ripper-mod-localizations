# TheRipper93 Community Translations

Community and machine translations for the [TheRipper93](https://www.patreon.com/theripper93) modules.

Foundry's [AI Content Policy](https://foundryvtt.com/article/ai-policy/) permits Machine-translated text only when someone who speaks the target language has reviewed it for accuracy before publication. That review cannot be guaranteed for community-submitted files, so every translation is collected here instead: a single module, installed from its manifest URL rather than through the official Foundry package listing.

Corrections and reviews from native speakers are always welcome!

## Installation

**From the manifest URL**

1. Open **Add-on Modules → Install Module**
2. Paste the manifest URL below into the **Manifest URL** field at the bottom of the dialog and click **Install**

   ```
   https://github.com/theripper93/ripper-mod-localizations/releases/latest/download/module.json
   ```

3. Enable **TheRipper93 Community Translations** in your world under **Manage Modules**
4. Pick your language in **Configure Settings → Core → Language**

## Contributing a translation

Every translated string lives in one file per module and language:

```
translations/<module-id>/<language>.json
```

So to translate **Better Roofs** into German, edit `translations/betterroofs/de.json`:

```jsonc
{
    "betterroofs": {                    // the module id, never change this key
        "tileConfig": {
            "showInFog.name": "Lüftet den Nebel des Krieges",
            "showInFog.hint": "Wenn ein Charakter Sicht auf dieses Feld hat, …"
        }
    }
}
```

- Keys you don't translate can simply be left out; missing keys and empty strings are fine.
- Don't paste English as a placeholder: a value identical to the English source counts as untranslated, and the tools drop it and translate it again.
- Keep placeholders, tags and references exactly as they are: `%s`, `%n`, `{name}`, HTML tags, `@UUID[…]`.
- 4-space indentation, UTF-8, and the module id always stays the top-level key.
- **Never edit `languages/`**: those files are generated from `translations/` by `merge_translations.py` when a release is built.

Then open a pull request. Corrections to a single string are just as welcome as a whole new language.