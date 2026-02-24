# -----------------------------------------------------------------------------
# Copyright (C) 2025 Iliyas Jorio.
# This file is part of GitFourchette, distributed under the GNU GPL v3.
# For full terms, see the included LICENSE file.
# -----------------------------------------------------------------------------

import logging
from collections.abc import Iterable
from typing import Any, overload

from gitfourchette import settings
from gitfourchette.gitdriver import GitDelta
from gitfourchette.localization import *
from gitfourchette.nav import NavContext
from gitfourchette.porcelain import *
from gitfourchette.qt import *
from gitfourchette.toolbox import *
from gitfourchette.trtables import TrTables

logger = logging.getLogger(__name__)

STATUS_ICON_LETTERS = "xadmrxxatxu"
"""
Table of status_*.svg icons for each enum entry
in pygit2.enums.DeltaStatus.
"""


def deltaModeText(om: FileMode, nm: FileMode) -> str:
    if om != 0 and nm != 0 and om != nm:
        # Mode change
        if nm == FileMode.BLOB_EXECUTABLE:
            return "+x"
        elif om == FileMode.BLOB_EXECUTABLE:
            return "-x"
        elif nm != FileMode.BLOB:
            return TrTables.shortFileModes(nm)
        else:
            return ""
    elif om == 0:
        # New file
        return TrTables.shortFileModes(nm)
    return ""


def fileTooltip(repo: Repo, delta: GitDelta, navContext: NavContext, isCounterpart: bool = False):
    locale = QLocale()
    of = delta.old
    nf = delta.new
    sc = delta.status

    text = "<table style='white-space: pre'>"

    def newLine(heading, caption):
        colon = _(':')
        color = mutedToolTipColorHex()
        return f"<tr><td style='color:{color}; text-align: right;'>{heading}{colon} </td><td>{caption}</td>"

    if sc == 'R':
        text += newLine(_("old name"), escape(of.path))
        text += newLine(_("new name"), escape(nf.path))
    else:
        text += newLine(_("name"), escape(nf.path))

    # Status caption
    statusCaption = TrTables.diffStatusChar(sc)
    if sc not in '?U':  # show status char except for untracked and conflict
        statusCaption += f" ({sc})"
    if sc == 'U':  # conflict sides
        assert delta.conflict is not None
        postfix = TrTables.enum(delta.conflict.sides)
        statusCaption += f" ({postfix})"
    text += newLine(_("status"), statusCaption)

    # Similarity + Old name
    if sc == 'R':
        text += newLine(_("similarity"), f"{delta.similarity}%")

    # File Mode
    if sc in 'DU':
        pass
    elif sc in 'A?':
        text += newLine(_("file mode"), TrTables.enum(nf.mode))
    elif of.mode != nf.mode:
        text += newLine(_("file mode"), f"{TrTables.enum(of.mode)} \u2192 {TrTables.enum(nf.mode)}")

    mTimeNS, size = -1, -1
    sizeIsAccurate = False
    if nf.isBlob() and not nf.isId0():
        # Stat workdir file (get size on disk, modification time)
        if navContext.isWorkdir():
            mTimeNS, size = nf.stat(repo)

        # Get accurate size in index if it's not an unstaged file
        if navContext in (NavContext.STAGED, NavContext.COMMITTED):
            assert nf.isIdValid()
            size = repo.peel_blob(nf.id).size
            sizeIsAccurate = True

    # Size (if applicable)
    if size != -1:
        sizeText = locale.formattedDataSize(size, 1)
        if not sizeIsAccurate:
            sizeText = _("{size} on disk", size=sizeText)
        text += newLine(_("size"), sizeText)

    # Modified time
    if mTimeNS != -1:
        timeSecs = int(mTimeNS * 1e-9)  # Convert from nanoseconds
        timeQdt = QDateTime.fromSecsSinceEpoch(timeSecs)
        timeText = locale.toString(timeQdt, settings.prefs.shortTimeFormat)
        text += newLine(_("modified"), timeText)

    # Blob/Commit IDs
    # (Not for unmerged conflicts)
    # (Not for untracked trees - those never have a valid ID)
    if sc != 'U' and nf.mode != FileMode.TREE:
        oldId = shortHash(of.id) if of.isIdValid() else _("(not computed)")
        newId = shortHash(nf.id) if nf.isIdValid() else _("(not computed)")
        idLegend = _("commit hash") if nf.mode == FileMode.COMMIT else _("blob hash")
        text += newLine(idLegend, f"{oldId} \u2192 {newId}")

    if isCounterpart:
        if navContext == NavContext.UNSTAGED:
            counterpartText = _("Currently viewing diff of staged changes in this file; "
                                "it also has <u>unstaged</u> changes.")
        else:
            counterpartText = _("Currently viewing diff of unstaged changes in this file; "
                                "it also has <u>staged</u> changes.")
        text += f"<p>{counterpartText}</p>"

    return text


class FileNode:
    def __init__(self, name: str, parent: FileNode | None, isFolder: bool):
        self.name = name
        self.parent = parent
        self.isFolder = isFolder
        self.children: list[FileNode] = []
        self.delta: GitDelta | None = None
        if parent and parent.fullPath:
            self.fullPath = f"{parent.fullPath}/{name}"
        else:
            self.fullPath = name

    def row(self):
        if self.parent:
            return self.parent.children.index(self)
        return 0


class FileListModel(QAbstractItemModel):
    class Role:
        Delta = Qt.ItemDataRole(Qt.ItemDataRole.UserRole + 0)
        FilePath = Qt.ItemDataRole(Qt.ItemDataRole.UserRole + 1)

    fileNodes: dict[str, FileNode]
    rootNode: FileNode
    highlightedCounterpartPath: str
    navContext: NavContext

    def __init__(self, parent: QWidget, navContext: NavContext):
        super().__init__(parent)
        self.navContext = navContext
        self.clear()

    @property
    def repo(self) -> Repo:
        return self.parent().repo

    @property
    def parentWidget(self) -> QWidget:
        parentWidget = self.parent()
        assert isinstance(parentWidget, QWidget)
        return parentWidget

    def clear(self):
        self.fileNodes = {}
        self.rootNode = FileNode("", None, True)
        self.highlightedCounterpartPath = ""
        self.modelReset.emit()

    def setContents(self, deltas: Iterable[GitDelta]):
        self.beginResetModel()

        self.fileNodes.clear()
        self.rootNode = FileNode("", None, True)

        sortedDeltas = sorted(deltas, key=lambda d: naturalSort(d.new.path))
        tree = settings.prefs.pathDisplayStyle == PathDisplayStyle.Tree

        for delta in sortedDeltas:
            if not tree:
                node = FileNode(delta.new.path, self.rootNode, False)
                node.delta = delta
                self.rootNode.children.append(node)
                self.fileNodes[delta.new.path] = node
            else:
                parts = delta.new.path.split('/')
                current = self.rootNode
                for part in parts[:-1]:
                    nextNode = None
                    for child in current.children:
                        if child.name == part and child.isFolder:
                            nextNode = child
                            break
                    if not nextNode:
                        nextNode = FileNode(part, current, True)
                        current.children.append(nextNode)
                        self.fileNodes[nextNode.fullPath] = nextNode
                    current = nextNode

                fname = parts[-1]
                node = FileNode(fname, current, False)
                node.delta = delta
                current.children.append(node)
                self.fileNodes[delta.new.path] = node

        self.endResetModel()

    def index(self, row: int, column: int = 0, parent: QModelIndex = QModelIndex_default) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()

        parentNode = parent.internalPointer() if parent.isValid() else self.rootNode
        if row < len(parentNode.children):
            return self.createIndex(row, column, parentNode.children[row])
        return QModelIndex()

    @overload
    def parent(self, child: QModelIndex) -> QModelIndex: ...

    @overload
    def parent(self) -> QObject | None: ...

    def parent(self, child: QModelIndex | None = None):
        if not isinstance(child, QModelIndex):
            return super().parent()

        if not child.isValid():
            return QModelIndex()

        childNode = child.internalPointer()
        if not childNode:
            return QModelIndex()

        parentNode = childNode.parent

        if parentNode == self.rootNode or parentNode is None:
            return QModelIndex()

        return self.createIndex(parentNode.row(), 0, parentNode)

    def columnCount(self, parent: QModelIndex = QModelIndex_default) -> int:
        return 1

    def rowCount(self, parent: QModelIndex = QModelIndex_default) -> int:
        parentNode = parent.internalPointer() if parent.isValid() else self.rootNode
        if parentNode is None:
            return 0
        return len(parentNode.children)

    def data(self, index: QModelIndex, role: Qt.ItemDataRole = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None

        node: FileNode = index.internalPointer()

        if role == FileListModel.Role.Delta:
            return node.delta

        elif role == FileListModel.Role.FilePath:
            # TODO: Canonical path for submodules?
            return node.fullPath

        elif role == Qt.ItemDataRole.DisplayRole:
            if node.isFolder:
                return node.name

            # TODO: Canonical path for submodules?
            text = abbreviatePath(node.fullPath, settings.prefs.pathDisplayStyle)

            # Show important mode info in brackets
            if node.delta:
                modeInfo = deltaModeText(node.delta.old.mode, node.delta.new.mode)
                if modeInfo:
                    text = f"[{modeInfo}] {text}"

            return text

        elif role == Qt.ItemDataRole.DecorationRole:
            if node.isFolder:
                return stockIcon("SP_DirIcon")
            if node.delta:
                letter = node.delta.status
                if letter == "?":  # untracked, fake A
                    letter = "A"
                letter = letter.lower()
                return stockIcon(f"status_{letter}")
            return None

        elif role == Qt.ItemDataRole.ToolTipRole:
            isCounterpart = node.fullPath == self.highlightedCounterpartPath
            if node.isFolder:
                # TODO: Nicer tooltip for folders
                return f"<b>{escape(node.fullPath)}</b>"
            if node.delta:
                return fileTooltip(self.repo, node.delta, self.navContext, isCounterpart)
            return None

        elif role == Qt.ItemDataRole.SizeHintRole:
            return QSize(-1, self.parentWidget.fontMetrics().height())

        elif role == Qt.ItemDataRole.FontRole:
            if node.fullPath == self.highlightedCounterpartPath:
                font = self.parentWidget.font()
                font.setUnderline(True)
                return font

        return None

    def deltaCount(self) -> int:
        return sum(1 for delta in self.fileNodes.values() if delta.delta is not None)

    def getIndexForFile(self, path: str) -> QModelIndex:
        try:
            node = self.fileNodes[path]
            return self.createIndex(node.row(), 0, node)
        except KeyError:
            return QModelIndex()

    def getRowForFile(self, path: str) -> int:
        """
        Get the row number for the given path.
        Raise KeyError if the path is absent from this model.
        """
        node = self.fileNodes[path]
        return node.row()

    def getFileAtRow(self, row: int) -> str:
        """
        Get the path corresponding to the given row number.
        Return an empty string if the row number is invalid.
        """
        if row < 0 or row >= len(self.rootNode.children):
            return ""
        node = self.rootNode.children[row]
        return node.fullPath

    def hasFile(self, path: str) -> bool:
        """
        Return True if the given path is present in this model.
        """
        return path in self.fileNodes
