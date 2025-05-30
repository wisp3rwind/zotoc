#! /usr/bin/env python3

import os
import click
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Literal, Self, Union

import pikepdf

from cli import ask_yn, html_color_block, select


def list_annotations(pdf: pikepdf.Pdf) -> None:
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
def main() -> None:
    pass


@main.command()
@click.argument("path")
def list_pdf_annotations(path: Union[str, Path]) -> None:
    with pikepdf.open(path) as pdf:
        list_annotations(pdf)


@dataclass
class Annotation:
    text: str
    comment: str | None
    color: str  # HTML color
    page: int
    left: float
    bottom: float
    right: float
    top: float

    @classmethod
    def parse(
        cls,
        text: str,
        comment: str | None,
        color: str,
        position_json: str,
    ) -> Self:
        position = json.loads(position_json)
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

    def position_key(self) -> tuple[int, float, float]:
        # FIXME: Add an ordering mode that tries to deal with two-column pdfs
        return self.page, -self.top, self.left


@dataclass
class Item:
    level: int
    title: str
    page: int
    top: float | None
    left: float | None
    source: Literal["pdf", "annot"]
    _obj: pikepdf.OutlineItem | None

    @classmethod
    def from_annotation(cls, annot: Annotation, level: int = 0) -> Self:
        title = annot.text
        if annot.comment:
            title = f"{title} ({annot.comment})"

        return cls(
            level=level,
            title=title,
            page=annot.page,
            top=annot.top + 0.5 * (annot.top - annot.bottom),
            left=annot.left,
            source="annot",
            _obj=None,
        )

    @classmethod
    def from_pikepdf(cls, obj: pikepdf.OutlineItem, level: int = 0) -> Self:
        # FIXME: Properly parse destination (important to get decent sorting
        # when combining new and old outline items)
        return cls(
            level=level,
            title=obj.title,
            page=pikepdf.Page(obj.destination[0]).index,
            top=None,
            left=None,
            source="pdf",
            _obj=obj,
        )

    def update(self, level: int, title: str) -> None:
        self.level = level
        self.title = title
        if self._obj:
            self._obj.title = title

    @property
    def obj(self) -> pikepdf.OutlineItem:
        if self._obj is None:
            self._obj = pikepdf.OutlineItem(
                self.title,
                self.page,
                page_location=pikepdf.PageLocation.XYZ,
                top=self.top,
            )
        return self._obj

    def position_key(self) -> tuple[int, float, float]:
        # FIXME: Add an ordering mode that tries to deal with two-column pdfs
        return (
            self.page,
            (-self.top if self.top is not None else -1.0),
            (self.left if self.left is not None else -1.0),
        )


def edit_outline(items: list[Item]) -> list[Item]:
    items_orig = items[:]

    while True:
        with tempfile.NamedTemporaryFile("w+", suffix=".md") as f:
            f.writelines((
                "=" * (item.level + 1) + f" {item.title}  [p. {item.page}, {item.source}, id={id_}]\n"
                for id_, item in enumerate(items)
            ))
            f.file.close()

            # FIXME: Check whether executable, otherwise have some fallback
            # options (nano, xdg-open)
            editor = os.environ.get("EDITOR", "vim")
            subprocess.run([editor, f.name])

            items = []
            with open(f.name) as fres:
                for line in fres:
                    header_prefix, text = line.split(" ", maxsplit=1)

                    level = len(header_prefix) - 1
                    assert header_prefix == "=" * (level + 1)

                    title, meta = text.rsplit("[", maxsplit=1)
                    title = title.strip()
                    meta = meta.removesuffix("]")
                    id_ = None
                    for m in meta.split(","):
                        m = m.strip()
                        match = re.match("id=([0-9]+)", m)
                        if match:
                            id_ = int(match.group(1))
                    assert id_ is not None


                    # support reordering + deletion by building a new list here
                    item = items_orig[id_]
                    # Use new title and level, might be modified
                    item.update(level=level, title=title)
                    items.append(item)

        print_outline(items)

        # FIXME: Validate outline nesting here (to avoid late failure in
        # build_pikepdf_outline)

        if not ask_yn("Keep editing?"):
            return items


def build_pikepdf_outline(items: list[Item], out=None, level=0) -> list[pikepdf.OutlineItem]:
    if out is None:
        out = []

    while items:
        item = items[0]
        if item.level == level:
            # Append items at the current level
            items.pop(0)
            out.append(item.obj)
        elif item.level == level + 1:
            # Higher nesting, recurse
            children = build_pikepdf_outline(items, None, level + 1)
            out[-1].children.extend(children)
        elif item.level < level:
            # Back to lower nesting
            return out
        else:  # item.level >= level + 2
            # Skipped a level
            # FIXME: handle the error case in a user-friendly way
            raise RuntimeError("invalid outline levels")

    return out


def fetch_zotero_data(cite_key: str) -> tuple[list[Annotation], Path]:
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

    # Locate the attachment file
    attachment_name = attachmentPath.removeprefix("storage:")
    attachment_path = data_path / "storage" / attachment_key / attachment_name

    return annotations, attachment_path


def print_pikepdf_outline(items: list[pikepdf.OutlineItem], level: int = 0) -> None:
    if level == 0:
        print("=" * 40)

    for item in items:
        if item.destination is not None:
            print("\t" * level + f"{item.title} [p. {pikepdf.Page(item.destination[0]).index} ({item.destination[1]})]")
        else:
            print("\t" * level + f"{item.title} [action]")

        print_pikepdf_outline(item.children, level + 1)

    if level == 0:
        print("=" * 40)


def print_outline(items: Sequence[Item], level: int = 0) -> None:
    if level == 0:
        print("=" * 40)

    for item in items:
        print("\t" * item.level + f"{item.title} [p. {item.page}]")

    if level == 0:
        print("=" * 40)


def parse_pikepdf_outline(items: Sequence[pikepdf.OutlineItem], level: int = 0) -> list[Item]:
    result = []

    for item in items:
        result.append(Item.from_pikepdf(item, level=level))
        result.extend(parse_pikepdf_outline(item.children, level=level + 1))
        
    return result


@main.command()
@click.argument("cite-key")
def outline_from_annotations(cite_key: str) -> None:
    annotations, attachment_path = fetch_zotero_data(cite_key)
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

    items = [Item.from_annotation(a) for a in annotations]

    with pikepdf.open(attachment_path) as pdf:
        with pdf.open_outline() as outline:
            if outline.root:
                print("Attachment already contains an outline:")
                print_pikepdf_outline(outline.root)
                clear_outline = not ask_yn("Keep these entries?")
                if not clear_outline:
                    items.extend(parse_pikepdf_outline(outline.root))
                    items = sorted(items, key=Item.position_key)
            else:
                clear_outline = False

    items = edit_outline(items)

    # Convert flat `Item` list into `OutlineItem` tree
    outline_items = build_pikepdf_outline(items)
    logging.debug(outline_items)

    # Add outline to PDF and write to a temporary file
    with pikepdf.open(attachment_path) as pdf:
        with pdf.open_outline() as outline:
            if clear_outline:
                outline.root.clear()
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
