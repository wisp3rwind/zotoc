#! /usr/bin/env python3

import os
import click
from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile

import pikepdf

from cli import *


# Problem: Zotero annotations are not stored to PDF (which is a good thing!),
# so this can't work without exporting the PDF; which is annoying.
# -> should really write a Zotero plugin using pdf.js instead


def list_annotations(pdf):
    all_annots = defaultdict(list)

    for page in pdf.pages:
        try:
            # print(page["/Annots"])
            # print(page["/Annots"].__dir__())
            for annot in page["/Annots"]:
                # print("/T:", repr(annot["/T"]))
                try:
                    content = str(annot.get("/Contents", "empty"))
                    rgb = tuple(float(c) for c in annot.get("/C", [-1, -1, -1]))
                    all_annots[rgb].append(content)
                except KeyError:
                    pass
        except KeyError:
            pass

    for col, cont in all_annots.items():
        print(col)
        print(cont)
        print()


@click.group()
def main():
    pass


@main.command()
@click.argument("path")
def list_pdf_annotations(path):
    with pikepdf.open(path) as pdf:
        list_annotations(pdf)


@dataclass
class Annotation:
    text: str
    comment: str
    color: str
    page: int
    left: float
    bottom: float
    right: float
    top: float

    @classmethod
    def parse(cls, text, comment, color, position):
        position = json.loads(position)
        return cls(
            text=text,
            comment=comment,
            color=color,
            page=position["pageIndex"],
            left=position["rects"][0][0],
            bottom=position["rects"][0][1],
            right=position["rects"][0][2],
            top=position["rects"][0][3],
        )

    def position_key(self):
        return self.page, -self.top, self.left


@main.command()
@click.argument("cite-key")
def outline_from_annotations(cite_key):
    data_path = Path("~/Zotero").expanduser()

    zotero_db_path = data_path / "zotero.sqlite"
    con = sqlite3.connect(f'file:{zotero_db_path}?mode=ro', uri=True)
    bbt_db_path = data_path / "better-bibtex.sqlite"
    # https://github.com/retorquere/zotero-better-bibtex/issues/2684#issuecomment-1774151488
    con.execute(f'ATTACH DATABASE "file:{bbt_db_path}?mode=ro" AS betterbibtex')

    cur = con.cursor()

    # use bbt database to map cite key to Zotero item key
    itemKey, libraryID = cur.execute(
        """SELECT itemKey, libraryID
        FROM betterbibtex.citationkey
        WHERE citationkey = ?
        """,
        (cite_key,),
    ).fetchone()
    print(f"{itemKey=}, {libraryID=}")

    # items: itemID WHERE key = itemKey
    itemID = cur.execute(
        """SELECT itemID
        FROM items
        WHERE key = ?
        """,
        (itemKey,),
    ).fetchone()[0]
    print(f"{itemID=}")

    # itemAttachments: itemID as attachmentID WHERE parentItemID = itemID
    attachments = cur.execute(
        """SELECT itemID as id, path
        FROM itemAttachments
        WHERE parentItemID = ?
        AND contentType == "application/pdf"
        """,
        (itemID,),
    ).fetchall()
    print(f"{attachments=}")

    idx, _ = select(
        "Choose attachment",
        [f"{id_}: {path}" for (id_, path) in attachments],
    )
    attachmentID, attachmentPath = attachments[idx]

    attachment_key = cur.execute(
        """SELECT key
        FROM items
        WHERE itemID = ?
        """,
        (attachmentID,),
    ).fetchone()[0]
    print(f"{attachment_key=}")

    # itemAnnotations: * WHERE parentItemID = attachmentID
    annotations = cur.execute(
        """SELECT text, comment, color, position
        FROM itemAnnotations
        WHERE parentItemID = ?
        """,
        (attachmentID,),
    ).fetchall()

    annotations = [Annotation.parse(*a) for a in annotations]
    annotations = sorted(annotations, key=Annotation.position_key)

    # Ask user to select annotations for a specific color
    annotations_by_color = defaultdict(list)
    for annot in annotations:
        annotations_by_color[annot.color].append(annot)

    options = []
    for color, annots in annotations_by_color.items():
        opt = f"Color: {html_color_block(color)}\n"
        opt += "\n".join([str(a) for a in annots])
        opt += "\n"
        options.append(opt)

    color_idx, _ = select("Choose annotation color", options)
    
    annotations = annotations_by_color[list(annotations_by_color.keys())[color_idx]]
    for a in annotations:
        print(a)

    # Edit outline
    # TODO: preview and ask whether to continue or keep editing
    annotations = [(0, a) for a in annotations]
    with tempfile.NamedTemporaryFile("w+", suffix=".md") as f:
        f.writelines((
            "=" * (level + 1) + f" {a.text}" + (f" ({a.comment})" if a.comment else "") + f" [p. {a.page}, id={id_}]\n"
            for id_, (level, a) in enumerate(annotations)
        ))
        f.file.close()

        editor = os.environ.get("EDITOR", "vim")
        subprocess.run([editor, f.name])

        annotations_new = []
        with open(f.name) as fres:
            for line in fres:
                header_prefix, text = line.split(" ", maxsplit=1)

                level = len(header_prefix) - 1
                assert header_prefix == "=" * (level + 1)

                text, meta = text.rsplit("[", maxsplit=1)
                text = text.removesuffix(" ")
                meta = meta.removesuffix("]")
                id_ = None
                for m in meta.split(","):
                    m = m.strip()
                    match = re.match("id=([0-9]+)", m)
                    if match:
                        id_ = int(match.group(1))
                assert id_ is not None


                # support reordering + deletion
                _, annot = annotations[id_]
                # Use new text, might be modified
                annot.text = text
                annotations_new.append((level, annot))

    print(annotations_new)

    outline_items = [
        (
            level,
            pikepdf.OutlineItem(
                annot.text,
                annot.page,
                page_location=pikepdf.PageLocation.XYZ,
                top=annot.top + 0.5 * (annot.top - annot.bottom),
            ),
        )
        for level, annot in annotations_new
    ]

    def build_outline(items, out=None, level=0):
        if out is None:
            out = []

        while items:
            item_level, item = items[0]
            if item_level == level:
                items.pop(0)
                out.append(item)
            elif item_level == level + 1:
                # FIXME: handle the error case in a user-friendly way
                children = build_outline(items, None, level + 1)
                out[-1].children.extend(children)
            elif item_level < level:
                return out
            else:
                raise RuntimeError("invalid outline levels")

        return out

    outline_items = build_outline(outline_items)

    print(outline_items)

    input("Continue?")

    # Locate the attachment file
    attachment_name = attachmentPath.removeprefix("storage:")
    attachment_path = data_path / "storage" / attachment_key / attachment_name

    # Add outline to PDF and write to a temporary file
    # FIXME: If outline exists, print and ask about overwriting (also, add an
    # option to merge with new items)
    with pikepdf.open(attachment_path) as pdf:
        with pdf.open_outline() as outline:
            outline.root.extend(outline_items)

        with tempfile.NamedTemporaryFile(
            dir=attachment_path.parent,
            suffix=".pdf",
        ) as dest:
            print(dest.name)
            pdf.save(dest.file)
            dest.file.close()

            subprocess.run(["xdg-open", dest.name])
    
            # TODO: Atomically copy to the original location, be sure to update mtime
            replace = ask_yn("Replace original file?")
            if replace:
                bak_path = attachment_path.with_name(attachment_path.name + ".bak")
                shutil.move(attachment_path, bak_path)
                shutil.move(dest.name, attachment_path)
                dest.delete_on_close = False
                try:
                    subprocess.run(["trash-put", bak_path])
                except Exception:
                    pass



if __name__ == "__main__":
    main()
