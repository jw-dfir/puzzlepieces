from __future__ import annotations

import argparse
import math
import sys
import time
import uuid
from collections import Counter, OrderedDict, defaultdict
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from PyQt6.QtCore import (
    QAbstractListModel,
    QLineF,
    QMimeData,
    QModelIndex,
    QPointF,
    QRectF,
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QAction, QColor, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGraphicsItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QGroupBox,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from audit import AuditLog
from engine import FILL_REASON, EngineSettings
from exporter import bounds, export_png, export_report
from jobs import run_process
from layouts import contact_sheet, native_bitmaps
from models import Assembly, Cancelled, IngestResult, Placement, Tile

MIME_TILE = "application/x-rdp-stitcher-tile"


def pixmap(tile: Tile) -> QPixmap:
    array = tile.rgb
    image = QImage(array.data, tile.width, tile.height, array.strides[0], QImage.Format.Format_RGB888)
    return QPixmap.fromImage(image.copy())


class TileModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.tiles: list[Tile] = []
        self.icons: OrderedDict[str, QPixmap] = OrderedDict()

    def set_tiles(self, tiles: list[Tile]) -> None:
        self.beginResetModel()
        self.tiles = tiles
        self.icons.clear()
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.tiles)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.tiles):
            return None
        tile = self.tiles[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            o = tile.occurrences[0]
            return f"{o.sequence:06d}  {tile.id[:8]}\n{Path(o.source).name}"
        if role == Qt.ItemDataRole.DecorationRole:
            if tile.id not in self.icons:
                self.icons[tile.id] = pixmap(tile).scaled(
                    48, 48, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation
                )
                if len(self.icons) > 512:
                    self.icons.popitem(last=False)
            return self.icons[tile.id]
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{tile.id}\n{len(tile.occurrences)} occurrence(s)\nDouble-click to place or drag onto canvas"
        if role == Qt.ItemDataRole.UserRole:
            return tile.id
        return None

    def flags(self, index):
        return super().flags(index) | Qt.ItemFlag.ItemIsDragEnabled

    def mimeTypes(self):
        return [MIME_TILE]

    def mimeData(self, indexes):
        mime = QMimeData()
        if indexes:
            mime.setData(MIME_TILE, self.tiles[indexes[0].row()].id.encode())
        return mime

    def supportedDragActions(self):
        return Qt.DropAction.CopyAction


class ClusterModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries = [("All regions", None)]

    def set_counts(self, counts):
        self.beginResetModel()
        self.entries = [("All regions", None)] + [
            (f"Region {key} ({counts[key]} tiles)", key) for key in sorted(counts)
        ]
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.entries)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.entries[index.row()][0]
        if role == Qt.ItemDataRole.UserRole:
            return self.entries[index.row()][1]
        return None


class Worker(QThread):
    progress = pyqtSignal(str, int, int)
    outcome = pyqtSignal(object)
    error = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, task, parent=None):
        super().__init__(parent)
        self.task = task
        self.cancel_event = Event()
        self._last_progress = 0.0

    def report(self, message, current, total):
        now = time.monotonic()
        if now - self._last_progress >= 0.10 or current == total:
            self._last_progress = now
            self.progress.emit(message, current, total)

    def run(self):
        try:
            self.outcome.emit(self.task(self.cancel_event, self.report))
        except Cancelled:
            self.cancelled.emit()
        except Exception as error:
            self.error.emit(f"{type(error).__name__}: {error}")


class TileItem(QGraphicsPixmapItem):
    def __init__(self, tile: Tile, placement: Placement, moved):
        super().__init__(pixmap(tile))
        self.tile_id = tile.id
        self.width, self.height = tile.width, tile.height
        self.moved = moved
        self._before = QPointF(placement.x, placement.y)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setShapeMode(QGraphicsPixmapItem.ShapeMode.BoundingRectShape)
        self.setTransformationMode(Qt.TransformationMode.FastTransformation)
        self.setPos(placement.x, placement.y)
        self.speculative = placement.speculative
        self.setToolTip(
            f"Tile {tile.id}\nScore: {placement.confidence if placement.confidence is not None else 'manual'}"
            + ("\nSPECULATIVE: placed by a guessed join" if self.speculative else "")
        )

    def paint(self, painter, option, widget=None):
        super().paint(painter, option, widget)
        if self.speculative:
            pen = QPen(QColor(255, 140, 0))
            pen.setWidthF(2)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawRect(QRectF(1, 1, self.width - 2, self.height - 2))

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            return QPointF(round(value.x() / 32) * 32, round(value.y() / 32) * 32)
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        self._before = self.pos()
        self.setZValue(1)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self.setZValue(0)
        if self.pos() != self._before:
            self.moved(self, self._before)


class Canvas(QGraphicsView):
    files_dropped = pyqtSignal(list)
    tile_dropped = pyqtSignal(str, object)
    crop_changed = pyqtSignal(object)

    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.grid = True
        self.space = False
        self.panning = False
        self.pan_start = None
        self.crop_mode = False
        self.crop_start = None
        self.crop_rect = None
        self.setAcceptDrops(True)
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate)
        self.setBackgroundBrush(QColor("#161c26"))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSceneRect(-10000, -10000, 20000, 20000)
        self.setOptimizationFlag(QGraphicsView.OptimizationFlag.DontSavePainterState)
        self.centerOn(0, 0)

    def drawBackground(self, painter, rect):
        painter.fillRect(rect, self.backgroundBrush())
        if not self.grid or self.transform().m11() < 0.20:
            return
        painter.setPen(QPen(QColor("#293244"), 0))
        step = 32 if self.transform().m11() >= 0.5 else 128
        left, top = math.floor(rect.left() / step) * step, math.floor(rect.top() / step) * step
        lines = [QLineF(x, rect.top(), x, rect.bottom()) for x in range(left, math.ceil(rect.right()), step)]
        lines += [QLineF(rect.left(), y, rect.right(), y) for y in range(top, math.ceil(rect.bottom()), step)]
        painter.drawLines(lines)

    def drawForeground(self, painter, rect):
        if self.crop_rect:
            painter.save()
            painter.setPen(QPen(QColor("#59d9b2"), 0, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(89, 217, 178, 30))
            painter.drawRect(self.crop_rect)
            painter.restore()

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.2 ** (event.angleDelta().y() / 120)
            current = self.transform().m11()
            target = max(0.03, min(24.0, current * factor))
            self.scale(target / current, target / current)
            event.accept()
        else:
            super().wheelEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self.space = True
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
        elif event.key() == Qt.Key.Key_Escape:
            self.crop_rect = None
            self.crop_start = None
            self.crop_changed.emit(None)
            self.viewport().update()
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self.space = False
            if not self.panning:
                self.unsetCursor()
            event.accept()
        else:
            super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        self.space = False
        self.panning = False
        self.unsetCursor()
        super().focusOutEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton or (
            self.space and event.button() == Qt.MouseButton.LeftButton
        ):
            self.panning = True
            self.pan_start = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        elif self.crop_mode and event.button() == Qt.MouseButton.LeftButton:
            self.crop_start = self.mapToScene(event.position().toPoint())
            self.crop_rect = QRectF(self.crop_start, self.crop_start)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.panning:
            current = event.position().toPoint()
            delta = current - self.pan_start
            self.pan_start = current
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            self._expand_scene()
            event.accept()
        elif self.crop_start is not None:
            self.crop_rect = QRectF(self.crop_start, self.mapToScene(event.position().toPoint())).normalized()
            self.viewport().update()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.panning:
            self.panning = False
            self.setCursor(Qt.CursorShape.OpenHandCursor if self.space else Qt.CursorShape.ArrowCursor)
            event.accept()
        elif self.crop_start is not None:
            self.crop_start = None
            self.crop_changed.emit(self.crop_rect)
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def _expand_scene(self):
        visible = self.mapToScene(self.viewport().rect()).boundingRect()
        if not self.sceneRect().adjusted(1000, 1000, -1000, -1000).contains(visible):
            self.setSceneRect(self.sceneRect().united(visible.adjusted(-5000, -5000, 5000, 5000)))

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(MIME_TILE) or event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event):
        if event.mimeData().hasFormat(MIME_TILE):
            self.tile_dropped.emit(
                bytes(event.mimeData().data(MIME_TILE)).decode(), self.mapToScene(event.position().toPoint())
            )
            event.acceptProposedAction()
        elif event.mimeData().hasUrls():
            self.files_dropped.emit([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
            event.acceptProposedAction()


class MainWindow(QMainWindow):
    def __init__(self, audit_dir: str | Path | None = None):
        super().__init__()
        self.setWindowTitle("Puzzle Pieces — by jwdfir")
        self.setWindowIcon(QIcon(str(Path(__file__).with_name("puzzle.png"))))
        self.resize(1450, 900)
        self.ingested = IngestResult()
        self.assembly = Assembly()
        self.by_id: dict[str, Tile] = {}
        self._inferred_by_id: dict[str, Tile] = {}
        self.link_index: dict[str, list] = {}
        self.items: dict[str, TileItem] = {}
        self.exports: list[dict] = []
        self.worker: Worker | None = None
        self._after_finished = None
        self._build_iterator = None
        self._build_done = None
        self._busy = False
        self._undo: list[tuple[str, Placement | None, Placement | None]] = []
        root = Path(audit_dir) if audit_dir else Path.home() / "RDP-Stitcher-Audits"
        label = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        self.audit = AuditLog(root / f"{label}.jsonl")
        self._build_ui()
        self.scene.selectionChanged.connect(self.show_properties)
        self.build_timer = QTimer(self)
        self.build_timer.timeout.connect(self._build_batch)
        self.statusBar().showMessage(f"Drop a folder or cache file • Audit: {self.audit.path}")

    def _build_ui(self):
        toolbar = self.addToolBar("Workspace")
        actions = [
            ("Open directory", self.choose_directory, "Ctrl+O"),
            ("Open files", self.choose_files, "Ctrl+Shift+O"),
            ("Fit canvas", self.fit_canvas, "F"),
            ("Undo", self.undo, "Ctrl+Z"),
            ("Return selected to sidebar", self.unplace_selected, "Delete"),
        ]
        self.mutation_actions = []
        for label, callback, shortcut in actions:
            action = QAction(label, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(callback)
            toolbar.addAction(action)
            if label != "Fit canvas":
                self.mutation_actions.append(action)
        grid = QAction("Grid", self)
        grid.setCheckable(True)
        grid.setChecked(True)
        grid.toggled.connect(lambda value: (setattr(self.view, "grid", value), self.view.viewport().update()))
        toolbar.addAction(grid)
        self.crop_action = QAction("Draw crop", self)
        self.crop_action.setCheckable(True)
        self.crop_action.toggled.connect(lambda value: setattr(self.view, "crop_mode", value))
        toolbar.addAction(self.crop_action)

        splitter = QSplitter()
        self.setCentralWidget(splitter)
        left = QWidget()
        layout = QVBoxLayout(left)
        self.sidebar_label = QLabel("Unplaced tiles")
        layout.addWidget(self.sidebar_label)
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Sequence / metadata groups", "Colour similarity"])
        self.sort_combo.currentIndexChanged.connect(self.refresh_sidebar)
        layout.addWidget(self.sort_combo)
        self.model = TileModel(self)
        self.sidebar = QListView()
        self.sidebar.setModel(self.model)
        self.sidebar.setUniformItemSizes(True)
        self.sidebar.setLayoutMode(QListView.LayoutMode.Batched)
        self.sidebar.setBatchSize(100)
        self.sidebar.setDragEnabled(True)
        self.sidebar.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.sidebar.doubleClicked.connect(self.place_from_sidebar)
        layout.addWidget(self.sidebar)
        instructions = QLabel(
            "Drop a folder to import and stitch.\nDrag tiles to correct; corrections pin.\nMiddle / Space-drag: pan\nCtrl+wheel: zoom\nDelete: return selected tiles\nEsc: clear crop"
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        splitter.addWidget(left)

        self.scene = QGraphicsScene(self)
        self.scene.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.BspTreeIndex)
        self.view = Canvas(self.scene)
        self.view.files_dropped.connect(self.import_paths)
        self.view.tile_dropped.connect(self.place_tile)
        self.view.crop_changed.connect(
            lambda value: self.statusBar().showMessage("Crop ready" if value else "Crop cleared")
        )
        splitter.addWidget(self.view)

        right = QWidget()
        controls = QVBoxLayout(right)
        assembly_box = QGroupBox("Automated assembly")
        form = QFormLayout(assembly_box)
        self.layout_view = QComboBox()
        self.layout_view.addItems(
            [
                "Inferred reconstruction (regions)",
                "Contact sheet: index only, not a reconstruction",
                "Native cached bitmaps: not a reconstruction",
            ]
        )
        self.layout_view.currentIndexChanged.connect(self.change_layout)
        form.addRow("Canvas view", self.layout_view)
        defaults = EngineSettings()
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(0.50, 0.999)
        self.threshold.setDecimals(3)
        self.threshold.setSingleStep(0.01)
        self.threshold.setValue(defaults.confidence)
        self.threshold.setToolTip(
            "Minimum posterior for a merge, combining calibrated seam evidence, measured cache-order structure and "
            "every competing alternative. Lower values join more and introduce more false joins."
        )
        form.addRow("Join confidence", self.threshold)
        self.margin = QDoubleSpinBox()
        self.margin.setRange(0.01, 1.0)
        self.margin.setDecimals(2)
        self.margin.setSingleStep(0.05)
        self.margin.setValue(defaults.visual_prior)
        self.margin.setToolTip(
            "Probability mass, spread over all records, that a neighbour is present but unexplained by cache order."
        )
        form.addRow("Visual prior", self.margin)
        self.candidates = QSpinBox()
        self.candidates.setRange(4, 64)
        self.candidates.setValue(16)
        form.addRow("Edge candidates", self.candidates)
        self.background = QCheckBox("Match uniform background records")
        self.background.toggled.connect(self.refresh_sidebar)
        form.addRow(self.background)
        self.fill = QCheckBox("Fill bounded background holes")
        self.fill.setChecked(True)
        self.fill.setToolTip("Place repeated uniform cached tiles into holes bounded by matching neighbours")
        form.addRow(self.fill)
        self.speculative = QCheckBox("Speculative full assembly (guesses)")
        self.speculative.setToolTip(
            "After confident joins run out, keep joining the best remaining non-contradicted fits to build the largest "
            "image possible. Guessed tiles are outlined in orange and flagged in exports and reports."
        )
        form.addRow(self.speculative)
        self.auto = QCheckBox("Auto-stitch after import")
        self.auto.setChecked(True)
        form.addRow(self.auto)
        self.run_button = QPushButton("Run Auto-Stitch Linker")
        self.run_button.clicked.connect(self.run_stitch)
        form.addRow(self.run_button)
        self.unpin_button = QPushButton("Unpin selected corrections")
        self.unpin_button.clicked.connect(self.unpin_selected)
        form.addRow(self.unpin_button)
        self.cluster_combo = QComboBox()
        self.cluster_model = ClusterModel(self)
        self.cluster_combo.setModel(self.cluster_model)
        self.cluster_combo.currentIndexChanged.connect(self.focus_cluster)
        form.addRow("Region", self.cluster_combo)
        controls.addWidget(assembly_box)

        regions_box = QGroupBox("Reconstructed regions")
        regions_layout = QVBoxLayout(regions_box)
        self.region_list = QListWidget()
        self.region_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.region_list.setIconSize(QPixmap(150, 90).size())
        self.region_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.region_list.setWordWrap(True)
        self.region_list.setMinimumHeight(240)
        self.region_list.setSpacing(4)
        self.region_list.currentItemChanged.connect(self.select_region)
        regions_layout.addWidget(self.region_list)
        self.export_region_button = QPushButton("Export selected region PNG")
        self.export_region_button.clicked.connect(self.export_region)
        regions_layout.addWidget(self.export_region_button)
        controls.addWidget(regions_box)

        self.properties = QLabel("Select a tile to inspect provenance.")
        self.properties.setWordWrap(True)
        self.properties.setTextFormat(Qt.TextFormat.PlainText)
        self.properties.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        properties_box = QGroupBox("Tile properties")
        prop_layout = QVBoxLayout(properties_box)
        prop_layout.addWidget(self.properties)
        controls.addWidget(properties_box)

        output = QGroupBox("Lossless export")
        output_form = QFormLayout(output)
        self.export_mode = QComboBox()
        self.export_mode.addItems(
            [
                "Full canvas",
                "Drawn crop",
                "Selected tiles",
                "Current region",
                "All regions individually",
                "All unique tiles — contact sheet",
            ]
        )
        output_form.addRow("Area", self.export_mode)
        self.export_scale = QSpinBox()
        self.export_scale.setRange(1, 8)
        self.export_scale.setValue(1)
        output_form.addRow("Integer scale", self.export_scale)
        self.export_button = QPushButton("Export PNG + audit report")
        self.export_button.clicked.connect(self.export)
        output_form.addRow(self.export_button)
        self.report_button = QPushButton("Export audit report only")
        self.report_button.clicked.connect(self.save_report)
        output_form.addRow(self.report_button)
        self.warning_button = QPushButton("View import warnings")
        self.warning_button.clicked.connect(self.show_warnings)
        output_form.addRow(self.warning_button)
        controls.addWidget(output)
        self.stats = QLabel("No tiles imported.")
        self.stats.setWordWrap(True)
        controls.addWidget(self.stats)
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        controls.addWidget(self.progress_bar)
        self.cancel_button = QPushButton("Cancel background task")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_task)
        controls.addWidget(self.cancel_button)
        controls.addStretch()
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setWidget(right)
        right_scroll.setMinimumWidth(285)
        right_scroll.setMaximumWidth(390)
        splitter.addWidget(right_scroll)
        splitter.setSizes([270, 890, 310])
        splitter.setStretchFactor(1, 1)
        right.setMinimumWidth(285)
        right.setMaximumWidth(370)
        self.mutating_widgets = [
            self.layout_view,
            self.run_button,
            self.unpin_button,
            self.background,
            self.sidebar,
            self.export_button,
            self.report_button,
            self.export_region_button,
        ]

    def set_busy(self, busy: bool):
        self._busy = busy
        for widget in self.mutating_widgets:
            widget.setEnabled(not busy)
        for action in self.mutation_actions:
            action.setEnabled(not busy)
        self.cancel_button.setEnabled(busy and self.worker is not None)
        self.view.setInteractive(not busy)
        self.progress_bar.setRange(0, 100 if not busy else 0)
        if not busy:
            self.progress_bar.setValue(100)

    def _start(self, task, callback):
        if self._busy:
            return
        worker = Worker(task, self)
        self.worker = worker
        worker.progress.connect(self.on_progress)
        worker.outcome.connect(callback)
        worker.error.connect(self.on_error)
        worker.cancelled.connect(lambda: self.statusBar().showMessage("Task cancelled; existing workspace retained"))
        worker.finished.connect(lambda: self._worker_finished(worker))
        self.set_busy(True)
        worker.start()

    def _worker_finished(self, worker):
        if self.worker is worker:
            self.worker = None
        worker.deleteLater()
        if not self.build_timer.isActive():
            self.set_busy(False)
        else:
            self.cancel_button.setEnabled(False)
        if self._after_finished:
            callback, self._after_finished = self._after_finished, None
            QTimer.singleShot(0, callback)

    def on_progress(self, message, current, total):
        self.progress_bar.setRange(0, max(total, 1))
        self.progress_bar.setValue(current)
        self.statusBar().showMessage(f"{message}: {current:,}/{total:,}")

    def on_error(self, error):
        self.audit.record("error", detail=error)
        QMessageBox.critical(self, "Operation failed", error)

    def cancel_task(self):
        if self.worker:
            self.worker.cancel_event.set()
            self.cancel_button.setEnabled(False)
            self.statusBar().showMessage("Cancelling at the next processing boundary…")

    def choose_directory(self):
        path = QFileDialog.getExistingDirectory(self, "Select cache/tile directory")
        if path:
            self.import_paths([path])

    def choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select cache/tile files", "", "RDP tiles/caches (*.bmp *.bin *.bmc)"
        )
        if paths:
            self.import_paths(paths)

    def import_paths(self, paths):
        if self._busy or not paths:
            return
        self.audit.record("import_requested", paths=paths)

        def task(cancel, progress):
            result = run_process("ingest", {"paths": paths}, cancel, progress)
            for source in result.sources:
                self.audit.record("source_ingested", **source)
            return result

        self._start(task, self._imported)

    def _imported(self, result):
        self.ingested = result
        self.by_id = {tile.id: tile for tile in result.tiles}
        self._inferred_by_id = dict(self.by_id)
        self.link_index = {}
        self.assembly = Assembly(unplaced=[t.id for t in result.active()])
        self._inferred = self.assembly
        self.layout_view.blockSignals(True)
        self.layout_view.setCurrentIndex(0)
        self.layout_view.blockSignals(False)
        self.exports = []
        self._undo.clear()
        self.view.crop_rect = None
        self.audit.record(
            "import_completed",
            source_count=len(result.sources),
            raw=result.raw_count,
            unique=len(result.tiles),
            duplicates=result.duplicate_count,
            suppressed=result.suppressed_count,
            warnings=result.warnings,
        )
        self._schedule_scene(self._import_scene_done)

    def _import_scene_done(self):
        if self.auto.isChecked():
            if self.worker is not None:
                self._after_finished = self.run_stitch
            else:
                QTimer.singleShot(0, self.run_stitch)

    def active_tiles(self):
        active = {t.id: t for t in self.ingested.active(self.background.isChecked())}
        for key in self.assembly.placements:
            if key in self.by_id:
                active[key] = self.by_id[key]
        return list(active.values())

    def change_layout(self, mode):
        if self._busy or not self.ingested.tiles:
            return
        if mode == 0:
            self.by_id = dict(self._inferred_by_id)
            self.assembly = self._inferred
            self._schedule_scene(self.fit_canvas)
            return
        if self.layout_view.currentIndex() != 0 and self.assembly.settings.get("layout_kind", "inferred") == "inferred":
            self._inferred = self.assembly
        if mode == 1:
            tiles = self.ingested.active(self.background.isChecked())

            def task(cancel, progress):
                return tiles, contact_sheet(tiles, cancel)
        else:

            def task(cancel, progress):
                return native_bitmaps(self.ingested, cancel)

        def ready(result):
            tiles, self.assembly = result
            self.by_id = {tile.id: tile for tile in tiles}
            self._undo.clear()
            self.audit.record("layout_changed", kind=self.assembly.settings["layout_kind"], tiles=len(tiles))
            self._schedule_scene(self.fit_canvas)

        self._start(task, ready)

    def run_stitch(self):
        if self._busy or not self.ingested.tiles:
            return
        settings = EngineSettings(
            confidence=self.threshold.value(),
            visual_prior=self.margin.value(),
            candidates=self.candidates.value(),
            include_background=self.background.isChecked(),
            fill_backgrounds=self.fill.isChecked(),
            speculative=self.speculative.isChecked(),
        )
        # All tiles, including suppressed backgrounds, so that native records are rebuilt complete.
        tiles = self.ingested.tiles
        prior = self._inferred if self.layout_view.currentIndex() else self.assembly
        fixed = {key: p for key, p in prior.placements.items() if p.manual}
        self.audit.record("stitch_requested", settings=asdict(settings), pinned=list(fixed))
        self._start(
            lambda cancel, progress: run_process(
                "stitch", {"tiles": tiles, "settings": settings, "fixed": fixed}, cancel, progress
            ),
            self._stitched,
        )

    def _stitched(self, assembly):
        self.assembly = assembly
        self._inferred = assembly
        self.by_id = {tile.id: tile for tile in assembly.instances} or {t.id: t for t in self.ingested.tiles}
        self._inferred_by_id = dict(self.by_id)
        self.link_index = defaultdict(list)
        for link in assembly.links:
            self.link_index[link.a].append(link)
            if link.b != link.a:
                self.link_index[link.b].append(link)
        self.layout_view.blockSignals(True)
        self.layout_view.setCurrentIndex(0)
        self.layout_view.blockSignals(False)
        self._undo.clear()
        self.audit.record(
            "stitch_completed",
            placements=len(assembly.placements),
            unplaced=len(assembly.unplaced),
            seconds=assembly.elapsed_seconds,
            link_count=len(assembly.links),
            regions=assembly.settings.get("regions", []),
            seam_models=assembly.settings.get("seam_models"),
            cache_order_priors=assembly.settings.get("cache_order_priors"),
            evidence=assembly.settings.get("evidence_counts"),
        )
        self._schedule_scene(self.fit_canvas)

    def _schedule_scene(self, done=None):
        # Diff the scene in short slices so the GUI keeps servicing input.
        old_items = self.items
        self.items = {}
        desired = self.assembly.placements

        def operations():
            for key, item in old_items.items():
                if key not in desired:
                    yield "remove", key, item
                else:
                    yield "update", key, item
            for key in desired:
                if key not in old_items:
                    yield "add", key, None

        self._build_iterator = iter(operations())
        self._build_done = done
        self.set_busy(True)
        self.build_timer.start(0)

    def _build_batch(self):
        started = time.perf_counter()
        try:
            while time.perf_counter() - started < 0.006:
                action, key, item = next(self._build_iterator)
                if action == "remove":
                    self.scene.removeItem(item)
                elif action == "update":
                    p = self.assembly.placements[key]
                    item.setPos(p.x, p.y)
                    if item.speculative != p.speculative:  # reused item: refresh the guessed-join outline
                        item.speculative = p.speculative
                        item.update()
                    self.items[key] = item
                else:
                    item = TileItem(self.by_id[key], self.assembly.placements[key], self.tile_moved)
                    self.scene.addItem(item)
                    self.items[key] = item
                if action != "remove":
                    p = self.assembly.placements[key]
                    item.setToolTip(f"Tile {key}\nScore: {p.confidence if p.confidence is not None else 'manual'}")
                    self.audit.record(
                        "tile_placed",
                        tile_id=key,
                        x=p.x,
                        y=p.y,
                        confidence=p.confidence,
                        cluster=p.cluster,
                        manual=p.manual,
                    )
        except StopIteration:
            self.build_timer.stop()
            self._build_iterator = None
            self._refresh_blocks()
            self.refresh_sidebar()
            if self.worker is None:
                self.set_busy(False)
            callback, self._build_done = self._build_done, None
            if callback:
                callback()

    def refresh_sidebar(self, *_):
        if not hasattr(self, "model"):
            return
        if self.assembly.settings.get("layout_kind") == "native_bitmaps":
            candidates = list(self.by_id.values())
        elif self.assembly.instances:
            uniform = self.background.isChecked()
            candidates = [t for t in self.by_id.values() if uniform or not t.solid or t.id in self.assembly.placements]
        else:
            candidates = self.ingested.active(self.background.isChecked())
        unplaced = [tile for tile in candidates if tile.id not in self.assembly.placements]
        if self.sort_combo.currentIndex() == 1:
            remaining = {t.id for t in unplaced}
            unplaced = [self.by_id[key] for key in self.ingested.colour_order if key in remaining]
        self.model.set_tiles(unplaced)
        self.assembly.unplaced = [tile.id for tile in unplaced]
        self.sidebar_label.setText(f"Unplaced tiles ({len(unplaced):,})")
        result = self.ingested
        settings = self.assembly.settings
        regions = settings.get("regions") or []
        kind = settings.get("layout_kind", "inferred")
        lines = [
            f"View: {kind.replace('_', ' ')}" + ("" if kind == "inferred" else " (not a reconstruction)"),
            f"Raw 32-px subtiles: {result.raw_count:,}",
            f"Unique pixel tiles: {len(result.tiles):,} ({result.duplicate_count:,} duplicates)",
            f"Uniform backgrounds: {result.suppressed_count:,}",
        ]
        if kind == "inferred" and "records" in settings:
            lines += [
                f"Native records: {settings['records']:,}",
                f"Regions: {len(regions):,}; records joined: {sum(r['records'] for r in regions):,}",
                f"Largest region: {regions[0]['records'] if regions else 0} records",
                f"Background fills: {sum(r['background_fills'] for r in regions):,}",
            ]
            for model in settings.get("seam_models") or []:
                if model.get("tpr_llr_ge_2") is not None:
                    axis = "H" if "horizontal" in model["calibration"] else "V"
                    lines.append(
                        f"Seam model {axis}: TPR {model['tpr_llr_ge_2']:.0%} / FPR {model['fpr_llr_ge_2']:.1%} at LLR≥2"
                    )
                else:
                    lines.append("Seam model: uncalibrated fallback (too few full records)")
        lines += [
            f"Placed: {len(self.assembly.placements):,}",
            f"Warnings: {len(result.warnings)}",
            f"Last assembly: {self.assembly.elapsed_seconds:.2f}s",
        ]
        self.stats.setText("\n".join(lines))

    def _refresh_blocks(self):
        counts = Counter(p.cluster for p in self.assembly.placements.values() if p.cluster is not None)
        self._refresh_region_previews(counts)
        current = self.cluster_combo.currentData()
        self.cluster_combo.blockSignals(True)
        self.cluster_model.set_counts(counts)
        self.cluster_combo.setCurrentIndex(
            next((i for i, (_, key) in enumerate(self.cluster_model.entries) if key == current), 0)
        )
        self.cluster_combo.blockSignals(False)

    def _refresh_region_previews(self, counts):
        self.region_list.blockSignals(True)
        self.region_list.clear()
        if self.assembly.settings.get("layout_kind", "inferred") == "inferred":
            members = defaultdict(list)
            for key, p in self.assembly.placements.items():
                if p.cluster is not None and key in self.by_id:
                    members[p.cluster].append((self.by_id[key], p))
            summaries = {r["region"]: r for r in self.assembly.settings.get("regions") or []}
            for cluster in sorted(members, key=lambda c: (-len(members[c]), c))[:400]:
                tiles = members[cluster]
                left = min(p.x for _, p in tiles)
                top = min(p.y for _, p in tiles)
                width = max(p.x + t.width for t, p in tiles) - left
                height = max(p.y + t.height for t, p in tiles) - top
                image = QImage(width, height, QImage.Format.Format_RGB888)
                image.fill(QColor(40, 40, 48))
                painter = QPainter(image)
                for tile, p in tiles:
                    painter.drawPixmap(p.x - left, p.y - top, pixmap(tile))
                painter.end()
                preview = QPixmap.fromImage(image).scaled(
                    150, 90, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
                )
                summary = summaries.get(cluster, {})
                confidence = summary.get("min_confidence")
                label = f"Region {cluster}\n{counts.get(cluster, len(tiles))} tiles · {width}×{height}"
                if confidence is not None:
                    label += f"\nmin conf {confidence:.2f}"
                item = QListWidgetItem(QIcon(preview), label)
                item.setData(Qt.ItemDataRole.UserRole, cluster)
                item.setToolTip(
                    f"Inferred region {cluster}: {summary.get('records', len(tiles))} records, "
                    f"{summary.get('background_fills', 0)} background fills, "
                    f"cache sequence span {summary.get('sequence_span')}. Click to focus."
                )
                self.region_list.addItem(item)
        self.region_list.blockSignals(False)

    def select_region(self, current, _previous=None):
        if current is None:
            return
        cluster = current.data(Qt.ItemDataRole.UserRole)
        index = next((i for i, (_, key) in enumerate(self.cluster_model.entries) if key == cluster), 0)
        self.cluster_combo.setCurrentIndex(index)
        self.focus_cluster()
        # Select the whole region so it can be dragged as one piece against another region (manual join).
        self.scene.clearSelection()
        for key, item in self.items.items():
            placement = self.assembly.placements.get(key)
            if placement is not None and placement.cluster == cluster:
                item.setSelected(True)
        self.statusBar().showMessage(
            f"Region {cluster} selected: drag any of its tiles to move the whole region; drop it edge-to-edge against "
            "another region to join them manually (moved tiles are pinned)."
        )

    def export_region(self):
        if self.cluster_combo.currentData() is None:
            QMessageBox.information(self, "Select a region", "Click a region preview first.")
            return
        self.export_mode.setCurrentIndex(3)
        self.export()

    def fit_canvas(self):
        if not self.assembly.placements:
            self.view.resetTransform()
            self.view.centerOn(0, 0)
            return
        left, top, right, bottom = bounds(self.by_id, self.assembly.placements)
        rect = QRectF(left - 32, top - 32, right - left + 64, bottom - top + 64)
        self.view.setSceneRect(self.view.sceneRect().united(rect.adjusted(-5000, -5000, 5000, 5000)))
        self.view.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)

    def focus_cluster(self, *_):
        cluster = self.cluster_combo.currentData()
        if cluster is None:
            return
        placements = {key: p for key, p in self.assembly.placements.items() if p.cluster == cluster}
        if placements:
            left, top, right, bottom = bounds(self.by_id, placements)
            self.view.fitInView(
                QRectF(left - 32, top - 32, right - left + 64, bottom - top + 64), Qt.AspectRatioMode.KeepAspectRatio
            )

    def show_properties(self):
        selected = [item for item in self.scene.selectedItems() if isinstance(item, TileItem)]
        if not selected:
            self.properties.setText("Select a tile to inspect provenance.")
            return
        key = selected[0].tile_id
        if key not in self.by_id or key not in self.assembly.placements:
            return
        tile = self.by_id[key]
        p = self.assembly.placements[tile.id]
        o = tile.occurrences[0]
        links = self.link_index.get(key, [])
        fill = any(link.reason == FILL_REASON for link in links)
        if p.manual:
            score = "Manual placement"
        elif p.confidence is None:
            score = "No inferred join"
        else:
            score = f"Join confidence (posterior): {p.confidence:.3f}"
        if fill:
            unit = "Repeated instance of a uniform cached tile (background fill)"
        elif self.assembly.settings.get("layout_kind", "inferred") == "inferred" and self.assembly.instances:
            unit = "Native cached record instance"
        else:
            unit = "Tile representative"
        evidence = "\n".join(
            f"  {'→' if link.a == key else '←'} {link.reason} (seam LLR {link.margin:+.2f})"
            for link in links[:6]
            if link.reason != FILL_REASON
        )
        self.properties.setText(
            f"{len(selected)} selected\n{unit}\nID: {tile.id}\nSource: {o.source}\n"
            f"SHA-256: {o.source_sha256}\nOffset: {o.record_offset}\nCache key: {o.cache_key}\n"
            f"Sequence: {o.sequence}\nSubtile: {o.sub_x},{o.sub_y}\n"
            f"Dimensions: {tile.width}×{tile.height}\nOccurrences: {len(tile.occurrences)}\n"
            f"Coordinates: {p.x},{p.y}\n{score}\nPinned: {p.manual}\n"
            f"Region: {p.cluster}\nExplicit capture timestamp: {o.timestamp}"
            + (f"\nEvidence:\n{evidence}" if evidence else "")
        )

    def _occupied(self, key, x, y):
        tile = self.by_id[key]
        rect = QRectF(x, y, tile.width, tile.height)
        return any(
            other != key and rect.intersects(QRectF(p.x, p.y, self.by_id[other].width, self.by_id[other].height))
            for other, p in self.assembly.placements.items()
        )

    def place_from_sidebar(self, index):
        if index.isValid():
            self.place_tile(
                self.model.tiles[index.row()].id, self.view.mapToScene(self.view.viewport().rect().center())
            )

    def place_tile(self, key, point):
        if self._busy or key not in self.by_id:
            return
        x, y = round(point.x() / 32) * 32, round(point.y() / 32) * 32
        for distance in range(0, 1025):
            found = None
            for dx in range(-distance, distance + 1):
                for dy in {-distance, distance} if abs(dx) < distance else range(-distance, distance + 1):
                    if not self._occupied(key, x + dx * 32, y + dy * 32):
                        found = x + dx * 32, y + dy * 32
                        break
                if found:
                    break
            if found:
                x, y = found
                break
        else:
            self.statusBar().showMessage("No free grid cell near this location")
            return
        before = self.assembly.placements.get(key)
        after = Placement(key, x, y, None, None, True)
        self.assembly.placements[key] = after
        self._undo.append((key, before, after))
        if key in self.items:
            item = self.items[key]
            item.setPos(x, y)
            if item.speculative:  # a manual placement is a deliberate decision, not a guess
                item.speculative = False
                item.update()
        else:
            item = TileItem(self.by_id[key], after, self.tile_moved)
            self.scene.addItem(item)
            self.items[key] = item
        self.audit.record("manual_place", **asdict(after))
        self.refresh_sidebar()

    def tile_moved(self, item, before_point):
        changed = {
            key: visual
            for key, visual in self.items.items()
            if (int(visual.pos().x()), int(visual.pos().y()))
            != (self.assembly.placements[key].x, self.assembly.placements[key].y)
        }
        proposed = dict(self.assembly.placements)
        for key, visual in changed.items():
            proposed[key] = Placement(key, int(visual.pos().x()), int(visual.pos().y()), None, None, True)
        invalid = False
        for key in changed:
            p = proposed[key]
            rect = QRectF(p.x, p.y, self.by_id[key].width, self.by_id[key].height)
            for other, q in proposed.items():
                if other != key and other in self.by_id:
                    size = self.by_id[other]
                    if rect.intersects(QRectF(q.x, q.y, size.width, size.height)):
                        invalid = True
                        break
            if invalid:
                break
        if invalid:
            for key, visual in changed.items():
                p = self.assembly.placements[key]
                visual.setPos(p.x, p.y)
            self.statusBar().showMessage("Occupied grid cell; move reverted")
            return
        for key in changed:
            before, after = self.assembly.placements[key], proposed[key]
            self.assembly.placements[key] = after
            self._undo.append((key, before, after))
            self.audit.record("manual_move", previous=asdict(before), placement=asdict(after))
            item = self.items[key]
            if item.speculative:  # a manual move is a deliberate decision, not a guess
                item.speculative = False
                item.update()
        self._refresh_blocks()
        self.view._expand_scene()
        self.show_properties()

    def unplace_selected(self):
        if self._busy:
            return
        for item in list(self.scene.selectedItems()):
            if isinstance(item, TileItem):
                key = item.tile_id
                before = self.assembly.placements.pop(key)
                self._undo.append((key, before, None))
                self.scene.removeItem(item)
                del self.items[key]
                self.audit.record("manual_unplace", tile_id=key, previous=asdict(before))
        self._refresh_blocks()
        self.refresh_sidebar()

    def unpin_selected(self):
        if self._busy:
            return
        for item in self.scene.selectedItems():
            if isinstance(item, TileItem):
                before = self.assembly.placements[item.tile_id]
                after = replace(before, manual=False)
                self.assembly.placements[item.tile_id] = after
                self._undo.append((item.tile_id, before, after))
                self.audit.record("unpin", tile_id=item.tile_id)
        self.show_properties()

    def undo(self):
        if self._busy or not self._undo:
            return
        key, before, after = self._undo.pop()
        if before is None:
            self.assembly.placements.pop(key, None)
            if key in self.items:
                self.scene.removeItem(self.items.pop(key))
        else:
            if self._occupied(key, before.x, before.y):
                self._undo.append((key, before, after))
                self.statusBar().showMessage("Undo blocked by an occupied grid cell")
                return
            self.assembly.placements[key] = before
            if key in self.items:
                item = self.items[key]
                item.setPos(before.x, before.y)
                if item.speculative != before.speculative:
                    item.speculative = before.speculative
                    item.update()
            else:
                item = TileItem(self.by_id[key], before, self.tile_moved)
                self.scene.addItem(item)
                self.items[key] = item
        self.audit.record("undo", tile_id=key, restored=asdict(before) if before else None)
        self._refresh_blocks()
        self.refresh_sidebar()
        self.show_properties()

    def export(self):
        if self._busy or not self.ingested.tiles:
            return
        mode = self.export_mode.currentIndex()
        placements = dict(self.assembly.placements)
        tiles = list(self.by_id.values())
        kind = self.assembly.settings.get("layout_kind", "inferred")
        if mode == 5 or (mode == 0 and not placements):
            tiles = list(self.ingested.tiles)
            placements = contact_sheet(tiles).placements
            kind = "contact_sheet"
        crop = None
        if mode == 1:
            rect = self.view.crop_rect
            if rect is None or rect.isEmpty():
                QMessageBox.information(self, "Draw a crop", "Enable Draw crop and drag across the canvas first.")
                return
            crop = math.floor(rect.left()), math.floor(rect.top()), math.ceil(rect.right()), math.ceil(rect.bottom())
        elif mode == 2:
            selected = {item.tile_id for item in self.scene.selectedItems() if isinstance(item, TileItem)}
            placements = {key: p for key, p in placements.items() if key in selected}
        elif mode == 3:
            cluster = self.cluster_combo.currentData()
            placements = {key: p for key, p in placements.items() if cluster is not None and p.cluster == cluster}
        if not placements:
            QMessageBox.information(self, "No tiles selected", "Select tiles or choose a region to export.")
            return
        if mode == 4:
            directory = QFileDialog.getExistingDirectory(self, "Export blocks to directory")
            if not directory:
                return
            jobs = []
            clusters = sorted({p.cluster for p in placements.values() if p.cluster is not None})
            prefix = "rdp-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
            for cluster in clusters:
                jobs.append(
                    (
                        Path(directory) / f"{prefix}-region-{cluster:04d}.png",
                        {key: p for key, p in placements.items() if p.cluster == cluster},
                        None,
                    )
                )
            if not jobs:
                QMessageBox.information(self, "No regions", "No reconstructed regions are available.")
                return
            report_path = Path(directory) / f"{prefix}-report.json"
        else:
            filename, _ = QFileDialog.getSaveFileName(
                self, "Export lossless reconstruction", "reconstruction.png", "PNG (*.png)"
            )
            if not filename:
                return
            target = Path(filename)
            if target.suffix.lower() != ".png":
                target = target.with_suffix(".png")
            jobs = [(target, placements, crop)]
            report_path = target.with_suffix(".report.json")
        scale = self.export_scale.value()
        assembly = Assembly(
            dict(self.assembly.placements),
            list(self.assembly.links),
            list(self.assembly.unplaced),
            dict(self.assembly.settings),
            self.assembly.elapsed_seconds,
        )
        prior_exports = list(self.exports)

        def task(cancel, progress):
            outputs = []
            for index, (target, subset, rectangle) in enumerate(jobs):
                progress("Exporting PNG", index, len(jobs))
                output = export_png(target, tiles, subset, crop=rectangle, scale=scale, cancel=cancel, layout_kind=kind)
                outputs.append(output)
                self.audit.record("png_export", **output)
            self.audit.flush()
            export_report(report_path, self.ingested, assembly, self.audit.snapshot(), prior_exports + outputs)
            return outputs, str(report_path)

        self._start(task, self._exported)

    def _exported(self, result):
        outputs, report = result
        self.exports.extend(outputs)
        self.statusBar().showMessage(f"Exported {len(outputs)} PNG(s) and {report}")

    def save_report(self):
        if self._busy:
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Export forensic manifest", "rdp-report.json", "JSON (*.json)")
        if not filename:
            return
        self.audit.record("report_export", path=filename)
        self._start(
            lambda cancel, progress: export_report(
                filename, self.ingested, self.assembly, self.audit.snapshot(), self.exports
            ),
            lambda _: self.statusBar().showMessage(f"Report saved: {filename}"),
        )

    def show_warnings(self):
        message = QMessageBox(self)
        message.setWindowTitle("Import warnings")
        message.setText(f"{len(self.ingested.warnings)} import warning(s).")
        message.setDetailedText("\n".join(self.ingested.warnings) or "No warnings.")
        message.exec()

    def closeEvent(self, event):
        if self.worker is not None or self.build_timer.isActive():
            if self.worker:
                self.worker.cancel_event.set()
            self.statusBar().showMessage("Finishing background work before closing…")
            event.ignore()
            QTimer.singleShot(200, self.close)
            return
        self.audit.record("workspace_closed", placed=len(self.assembly.placements))
        self.audit.close()
        event.accept()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Puzzle Pieces — by jwdfir")
    parser.add_argument("paths", nargs="*", help="Folders, BMP tiles or BIN/BMC caches")
    parser.add_argument("--audit-dir", type=Path)
    args = parser.parse_args(argv)
    if sys.platform == "win32":  # show the Puzzle Pieces icon on the taskbar instead of Python's
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("jwdfir.PuzzlePieces")
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Puzzle Pieces")
    app.setWindowIcon(QIcon(str(Path(__file__).with_name("puzzle.png"))))
    app.setStyle("Fusion")
    window = MainWindow(args.audit_dir)
    window.show()
    if args.paths:
        QTimer.singleShot(0, lambda: window.import_paths(args.paths))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
