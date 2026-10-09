# differ

`differ [git diff arguments]` shows a git diff in Phabricator's Differential side by side view,
without a Phabricator server or an account. It writes an HTML page to `/tmp` and opens it with
`xdg-open`. The page is named after the repository and a hash of the diff, like
`differ-myproject-6Rq5iViF.html`, so the same diff always makes the same file.

    differ
    differ --staged
    differ HEAD~3

It needs Python 3, Pygments (`python-pygments`) and GNU `diff`. To have the `differ` command, link
the script into a directory on your `PATH`:

    ln -s "$PWD/differ.py" ~/.local/bin/differ

The script needs `style.css`, `script.js` and `fonts/` from this directory, so linking it is easier
than copying it.

## How it works

It is a port of the code Phabricator uses to render a diff, so the page looks and behaves like
Differential's:

- A list of the changed files at the top links to each file.
- Once a file's name scrolls away, a banner at the top of the window shows which file you're in.
- Removed and added lines are paired side by side, with the changed characters within each pair
  highlighted.
- Changes to leading and trailing whitespace are ignored, except in Python, Haskell and YAML files,
  where indentation matters.
- Moved and copied code is marked in the gutter.
- Long runs of unchanged lines are collapsed behind "Show All N Lines" bars, and deleted, generated
  and whitespace-only files behind "Show File Contents".

Paths are relative to the repository root, as `git diff` names them.

## Differences from Phabricator

- All languages are coloured by the installed Pygments. Phabricator has lexers of its own for
  Python, PHP and JSON, which are simpler and older, and its Pygments can be older too. So colours
  can differ, and Phabricator shows some languages as plain text, such as Rust, whose lexer name
  `rs` older versions of Pygments don't know.
- The page is only the list of files and the files themselves. There is no Phabricator navigation
  bar and no header.

## Files

- `differ.py`, the tool.
- `style.css`, Phabricator's stylesheets for the diff view.
- `script.js`, the page's script, which shows collapsed lines and shielded files when their links
  are clicked.
- `fonts/`, Lato, the font Phabricator's pages use, and the few file icons it uses from Font
  Awesome, both embedded in each page.

## License

Apache 2.0, the same as Phabricator, since `differ.py`, `script.js` and `style.css` are derived
from it. `NOTICE` credits Phacility's projects, the Lato fonts and Font Awesome. The fonts are under
the SIL Open Font License, and the Font Awesome rules in `style.css` under the MIT License.
