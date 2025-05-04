# zotoc

Tools for adding a TOC (a.k.a. outline) to PDFs that unfortunately lack them.
This implements a workflow along the lines of:

- Highlight all headlines in a document in a single color, which isn't used for
  other annotations.
- Get the [zotero-better-bibtex](https://github.com/retorquere/zotero-better-bibtex) citation key for that item.
- Quit Zotero before the next step, see below for an explanation.
- Run this tool, providing the citation key as first argument.
- Interactively select the color to use for the outline.

Then, the script will fetch all annotations with that color, and use the
highlighted text to create outline items with link target at the location of
the headlines.


# Development

Eventually, I would like this to be a Zotero plugin without any other
dependencies (i.e. using pdf.js and integrating nicely with the UI).
For now, it's a quick & dirty prototype built with Python and
pikepdf, reading the Zotero SQLite database to fetch the necessary data.
(The latter means that Zotero cannot be running while using this tool, 
otherwise SQLite will timeout on trying to open the locked database.)

This should be fairly safe to use: The database is opened ready-only, and
there's a prompt before overwriting the original file (of which a backup will 
be retained anyway).
