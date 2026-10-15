# -*- coding: utf-8 -*-
"""将棋 検討ツール（盤面で駒を動かすと、エンジンが候補手を出し、LLMが局面を解説する）"""
import json
import os
import queue
from datetime import datetime
import sys
import threading
import time
import traceback

from PyQt6.QtCore import QBuffer, QTimer, QByteArray, QIODevice, QPointF, QObject, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QFont, QKeySequence, QPainter, QPen, QPolygonF, QShortcut
from PyQt6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QDialogButtonBox, QFileDialog, QFormLayout,
                             QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow,
                             QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTextBrowser, QTextEdit,
                             QToolTip, QVBoxLayout, QWidget)
import shogi

import core

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
FONT_FAMILIES = ["Yu Gothic UI", "Meiryo UI", "Meiryo", "MS Gothic", "Noto Sans CJK JP"]


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def sq_of(name):
    return shogi.SQUARE_NAMES.index(name)


def safe(fn, default=False):
    """駒が欠けた局面などで例外が出ても、画面が止まらないようにする。"""
    try:
        return fn()
    except Exception:
        return default


def clipboard_png(max_width=1800):
    """クリップボードの画像をPNGのバイト列で返す（画像が無ければNone）。"""
    img = QApplication.clipboard().image()
    if img.isNull():
        return None
    if img.width() > max_width:
        img = img.scaledToWidth(max_width, Qt.TransformationMode.SmoothTransformation)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


def screen_png(screen, max_width=None):
    """指定した画面(モニター)全体を撮影して、PNGのバイト列で返す。"""
    img = screen.grabWindow(0).toImage()
    if img.isNull():
        return None
    if max_width and img.width() > max_width:
        img = img.scaledToWidth(max_width, Qt.TransformationMode.SmoothTransformation)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(ba)


PIECE_MENU = [("歩", "P"), ("香", "L"), ("桂", "N"), ("銀", "S"), ("金", "G"), ("角", "B"), ("飛", "R"), ("玉", "K"),
              ("と", "+P"), ("杏", "+L"), ("圭", "+N"), ("全", "+S"), ("馬", "+B"), ("龍", "+R")]


# ====================================================================== 盤面
class BoardWidget(QWidget):
    moveMade = pyqtSignal(str)       # 利用者が指した手（USI形式）
    positionEdited = pyqtSignal(str)  # 編集モードで直した局面（SFEN）

    def __init__(self):
        super().__init__()
        self.setMinimumSize(420, 480)
        self.board = shogi.Board()
        self.legal = list(self.board.legal_moves)
        self.last_move = None
        self.preview = None
        self.flipped = False
        self.sel = None          # ("sq", 升) または ("drop", 駒の種類)
        self.targets = {}        # 行き先の升 -> [指し手]
        self.hand_rects = []
        self.edit_mode = False
        self.names = ["先手", "後手"]      # 対局者の名前（棋譜に入っていれば盤の上下の帯に出す）

    def set_position(self, board, last_move=None):
        self.board = board
        self.legal = safe(lambda: list(board.legal_moves), [])
        self.last_move = last_move
        self.preview = None
        self._clear_sel()

    def set_preview(self, usi):
        self.preview = usi
        self.update()

    def _clear_sel(self):
        self.sel = None
        self.targets = {}
        self.update()

    # ---- 座標
    def _geom(self):
        w, h = self.width(), self.height()
        cell = max(20.0, min(w / 10.2, h / 11.7))
        ox, oy = (w - 9 * cell) / 2, (h - 11.7 * cell) / 2
        return {"cell": cell, "bx": ox, "by": oy + 1.5 * cell,
                "top": QRectF(ox, oy, 9 * cell, cell),
                "bottom": QRectF(ox, oy + 11.7 * cell - cell, 9 * cell, cell)}

    def _rc(self, sq):
        col, row = sq % 9, sq // 9
        return (8 - col, 8 - row) if self.flipped else (col, row)

    def _sq_from_rc(self, col, row):
        return (8 - row) * 9 + (8 - col) if self.flipped else row * 9 + col

    def _center(self, sq, g):
        col, row = self._rc(sq)
        return QPointF(g["bx"] + (col + 0.5) * g["cell"], g["by"] + (row + 0.5) * g["cell"])

    def _pos_to_sq(self, pos):
        g = self._geom()
        col = int((pos.x() - g["bx"]) // g["cell"])
        row = int((pos.y() - g["by"]) // g["cell"])
        if 0 <= col < 9 and 0 <= row < 9 and pos.x() >= g["bx"] and pos.y() >= g["by"]:
            return self._sq_from_rc(col, row)
        return None

    # ---- 描画
    def draw_piece(self, p, cx, cy, size, symbol, color):
        p.save()
        p.translate(cx, cy)
        # 画面の上側にいる側の駒を逆向きにする（盤面を反転したときは先手が上側になる）
        if (color == shogi.WHITE) != self.flipped:
            p.rotate(180)
        s = size
        poly = QPolygonF([QPointF(0, -0.46 * s), QPointF(0.34 * s, -0.28 * s), QPointF(0.42 * s, 0.46 * s),
                          QPointF(-0.42 * s, 0.46 * s), QPointF(-0.34 * s, -0.28 * s)])
        p.setBrush(QColor("#f7e3b5"))
        p.setPen(QPen(QColor("#5b4015"), 1.2))
        p.drawPolygon(poly)
        p.setPen(QColor("#c0182a") if symbol.startswith("+") else QColor("#1c1308"))
        f = QFont(self.font())
        f.setPixelSize(max(8, int(0.5 * s)))
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(-0.5 * s, -0.38 * s, s, 0.86 * s), Qt.AlignmentFlag.AlignCenter, core.BOARD_NAME[symbol])
        p.restore()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        g = self._geom()
        cell, bx, by = g["cell"], g["bx"], g["by"]
        p.fillRect(self.rect(), QColor("#ece7da"))
        p.fillRect(QRectF(bx, by, 9 * cell, 9 * cell), QColor("#e9c47e"))

        # 升目と目印
        for sq_hl, col in self._highlights():
            c = self._center(sq_hl, g)
            p.fillRect(QRectF(c.x() - cell / 2, c.y() - cell / 2, cell, cell), col)
        p.setPen(QPen(QColor("#3a2a10"), 1.2))
        for i in range(10):
            p.drawLine(QPointF(bx, by + i * cell), QPointF(bx + 9 * cell, by + i * cell))
            p.drawLine(QPointF(bx + i * cell, by), QPointF(bx + i * cell, by + 9 * cell))
        p.setBrush(QColor("#3a2a10"))
        for (cx, cy) in [(3, 3), (6, 3), (3, 6), (6, 6)]:
            p.drawEllipse(QPointF(bx + cx * cell, by + cy * cell), cell * 0.06, cell * 0.06)

        # 筋・段の数字
        f = QFont(self.font())
        f.setPixelSize(max(8, int(cell * 0.28)))
        p.setFont(f)
        p.setPen(QColor("#3a2a10"))
        for i in range(9):
            file_no = (i + 1) if self.flipped else (9 - i)
            rank_no = (9 - i) if self.flipped else (i + 1)
            p.drawText(QRectF(bx + i * cell, by - 0.45 * cell, cell, 0.4 * cell), Qt.AlignmentFlag.AlignCenter,
                       core.FILE_ZEN[file_no])
            p.drawText(QRectF(bx + 9 * cell + 2, by + i * cell, 0.5 * cell, cell), Qt.AlignmentFlag.AlignCenter,
                       core.RANK_KAN[rank_no])

        # 駒
        for sq in range(81):
            pc = self.board.piece_at(sq)
            if pc is not None:
                c = self._center(sq, g)
                self.draw_piece(p, c.x(), c.y(), cell * 0.92, pc.symbol().upper(), pc.color)

        # 行き先の目印
        for to_sq in self.targets:
            c = self._center(to_sq, g)
            if self.board.piece_at(to_sq) is not None:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(220, 40, 40, 200), cell * 0.07))
                p.drawEllipse(c, cell * 0.42, cell * 0.42)
            else:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(30, 110, 230, 120))
                p.drawEllipse(c, cell * 0.16, cell * 0.16)

        # 持ち駒
        self.hand_rects = []
        top_color = shogi.BLACK if self.flipped else shogi.WHITE
        for rect, color in ((g["top"], top_color), (g["bottom"], 1 - top_color)):
            self._draw_hand(p, rect, color, cell)
            f = QFont(self.font())
            f.setPixelSize(max(8, int(cell * 0.28)))
            p.setFont(f)
            p.setPen(QColor("#3a2a10"))
            nm = (self.names[0] if color == shogi.BLACK else self.names[1]) if len(self.names) >= 2 else ""
            if nm and nm not in ("先手", "後手"):
                f.setPixelSize(max(9, int(cell * 0.34)))
                f.setBold(True)
                p.setFont(f)
                label = ("▲" if color == shogi.BLACK else "△") + nm
            else:
                label = "▲先手の持ち駒" if color == shogi.BLACK else "△後手の持ち駒"
            p.drawText(rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, label)

        # 候補手の矢印
        if self.preview:
            self._draw_preview(p, g)

        # 編集モードの表示
        if self.edit_mode:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(230, 120, 20, 170), 1, Qt.PenStyle.DashLine))
            p.drawRect(g["top"])
            p.drawRect(g["bottom"])
            p.setPen(QPen(QColor(230, 120, 20), 4))
            p.drawRect(self.rect().adjusted(2, 2, -2, -2))
            f = QFont(self.font())
            f.setPixelSize(max(10, int(cell * 0.26)))
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(190, 80, 0))
            p.drawText(QRectF(12, 8, max(70.0, g["top"].x() - 16), cell * 0.9), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                       "編集中")
        p.end()

    def _highlights(self):
        out = []
        lm = self.last_move
        if lm:
            if lm[1] != "*":
                out.append((sq_of(lm[0:2]), QColor(255, 235, 120, 150)))
            out.append((sq_of(lm[2:4]), QColor(255, 235, 120, 150)))
        if self.sel and self.sel[0] == "sq":
            out.append((self.sel[1], QColor(120, 200, 120, 170)))
        return out

    def _draw_hand(self, p, rect, color, cell):
        x = rect.x() + 0.1 * cell
        for pt, _name in core.HAND_ORDER:
            n = self.board.pieces_in_hand[color].get(pt, 0)
            if not n:
                continue
            sym = shogi.Piece(pt, color).symbol().upper()
            w = cell * 0.95
            r = QRectF(x, rect.y(), w, rect.height())
            if self.sel == ("drop", pt) and color == self.board.turn:
                p.fillRect(r, QColor(120, 200, 120, 170))
            self.draw_piece(p, r.center().x(), r.center().y(), cell * 0.8, sym, color)
            if n > 1:
                f = QFont(self.font())
                f.setPixelSize(max(8, int(cell * 0.3)))
                p.setFont(f)
                p.setPen(QColor("#a01010"))
                p.drawText(QRectF(r.right() - 0.05 * cell, r.bottom() - 0.45 * cell, 0.5 * cell, 0.45 * cell),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"×{n}")
            self.hand_rects.append((r, color, pt))
            x += cell * 1.2

    def _draw_preview(self, p, g):
        usi, cell = self.preview, g["cell"]
        try:
            to_c = self._center(sq_of(usi[2:4]), g)
        except Exception:
            return
        if usi[1] == "*":
            # 駒を打つ手：持ち駒の駒から、打つマスへ矢印を引く
            p.fillRect(QRectF(to_c.x() - cell / 2, to_c.y() - cell / 2, cell, cell), QColor(30, 100, 230, 90))
            pt = core.BASE_PT.get(usi[0])
            for r, color, hpt in self.hand_rects:
                if color == self.board.turn and hpt == pt:
                    p.fillRect(r, QColor(30, 100, 230, 90))
                    self._arrow(p, r.center(), to_c, cell)
                    break
            return
        self._arrow(p, self._center(sq_of(usi[0:2]), g), to_c, cell)

    @staticmethod
    def _arrow(p, fr_c, to_c, cell):
        col = QColor(30, 100, 230, 170)
        p.setPen(QPen(col, cell * 0.12, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(fr_c, to_c)
        dx, dy = to_c.x() - fr_c.x(), to_c.y() - fr_c.y()
        L = max(1.0, (dx * dx + dy * dy) ** 0.5)
        ux, uy = dx / L, dy / L
        head = cell * 0.3
        a = QPointF(to_c.x() - ux * head + uy * head * 0.6, to_c.y() - uy * head - ux * head * 0.6)
        b = QPointF(to_c.x() - ux * head - uy * head * 0.6, to_c.y() - uy * head + ux * head * 0.6)
        p.setBrush(col)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(QPolygonF([to_c, a, b]))

    # ---- 操作
    def mousePressEvent(self, e):
        if self.edit_mode:
            if e.button() == Qt.MouseButton.LeftButton:
                self._edit_click(e.position())
            return
        if e.button() == Qt.MouseButton.RightButton:
            self._clear_sel()
            return
        pos = e.position()
        for rect, color, pt in self.hand_rects:
            if rect.contains(pos):
                if color == self.board.turn:
                    self._select_drop(pt)
                return
        sq = self._pos_to_sq(pos)
        if sq is None:
            self._clear_sel()
            return
        if sq in self.targets:
            self._try_move(sq)
            return
        pc = self.board.piece_at(sq)
        if pc is not None and pc.color == self.board.turn:
            self.sel = ("sq", sq)
            self.targets = {}
            for m in self.legal:
                if m.drop_piece_type is None and m.from_square == sq:
                    self.targets.setdefault(m.to_square, []).append(m)
            self.update()
        else:
            self._clear_sel()

    def _select_drop(self, pt):
        self.sel = ("drop", pt)
        self.targets = {}
        for m in self.legal:
            if m.drop_piece_type == pt:
                self.targets.setdefault(m.to_square, []).append(m)
        self.update()

    # ---- 編集モード
    def _edit_click(self, pos):
        g = self._geom()
        top_color = shogi.BLACK if self.flipped else shogi.WHITE
        if g["top"].contains(pos):
            self._edit_hand(top_color)
        elif g["bottom"].contains(pos):
            self._edit_hand(1 - top_color)
        else:
            sq = self._pos_to_sq(pos)
            if sq is not None:
                self._edit_square(sq)

    def _choose_piece(self):
        """置く駒を選ばせる。先手は大文字・後手は小文字の記号、空にするなら""、取り消しならNone。"""
        menu = QMenu(self)
        chosen = {}
        chosen[menu.addAction("空にする")] = ""
        for title, lower in (("先手の駒を置く", False), ("後手の駒を置く", True)):
            sub = menu.addMenu(title)
            for ja, sym in PIECE_MENU:
                chosen[sub.addAction(ja)] = sym.lower() if lower else sym
        return chosen.get(menu.exec(QCursor.pos()))

    def _edit_square(self, sq):
        sym = self._choose_piece()
        if sym is None:
            return
        cells, hands, turn = core.board_to_model(self.board)
        cells[sq] = sym or None
        self.positionEdited.emit(core.model_to_sfen(cells, hands, turn))

    def _hand_dialog(self, color, cur):
        dlg = QDialog(self)
        dlg.setWindowTitle("先手の持ち駒" if color == shogi.BLACK else "後手の持ち駒")
        form = QFormLayout(dlg)
        boxes = {}
        for pt, name in core.HAND_ORDER:
            sb = QSpinBox()
            sb.setRange(0, 18)
            sb.setValue(int(cur.get(pt, 0)))
            boxes[pt] = sb
            form.addRow(name, sb)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec():
            return {pt: sb.value() for pt, sb in boxes.items() if sb.value()}
        return None

    def _edit_hand(self, color):
        cells, hands, turn = core.board_to_model(self.board)
        new = self._hand_dialog(color, hands[color])
        if new is None:
            return
        hands[color] = new
        self.positionEdited.emit(core.model_to_sfen(cells, hands, turn))

    def _ask_promote(self):
        box = QMessageBox(self)
        box.setWindowTitle("成りますか？")
        yes = box.addButton("成る", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("成らない", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is yes

    def _try_move(self, sq):
        moves = self.targets[sq]
        if len(moves) == 1:
            m = moves[0]
        else:
            promo = next(x for x in moves if x.promotion)
            plain = next(x for x in moves if not x.promotion)
            m = promo if self._ask_promote() else plain
        self._clear_sel()
        self.moveMade.emit(m.usi())


# ====================================================================== 設定画面
class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("設定")
        self.setMinimumWidth(620)
        opts = dict(cfg.get("engine_options", {}))
        form = QFormLayout(self)

        self.engine = QLineEdit(cfg.get("engine_path", ""))
        b1 = QPushButton("参照…")
        b1.clicked.connect(self._pick_engine)
        form.addRow("エンジン（exe）", self._row(self.engine, b1))

        self.evaldir = QLineEdit(str(opts.pop("EvalDir", "")))
        self.evaldir.setPlaceholderText("評価関数（nn.bin）のあるフォルダ。空欄なら指定しない")
        b2 = QPushButton("参照…")
        b2.clicked.connect(self._pick_dir)
        form.addRow("評価関数フォルダ", self._row(self.evaldir, b2))

        self.threads = QSpinBox()
        self.threads.setRange(1, 256)
        self.threads.setValue(int(opts.pop("Threads", 4)))
        form.addRow("スレッド数", self.threads)
        self.hash = QSpinBox()
        self.hash.setRange(16, 131072)
        self.hash.setValue(int(opts.pop("USI_Hash", 512)))
        form.addRow("ハッシュ(MB)", self.hash)

        self.extra = QPlainTextEdit("\n".join(f"{k}={v}" for k, v in opts.items()))
        self.extra.setPlaceholderText("追加のエンジン設定を1行に1つ「名前=値」で書く（例：FV_SCALE=24）")
        self.extra.setFixedHeight(70)
        form.addRow("追加オプション", self.extra)

        self.sec = QSpinBox()
        self.sec.setRange(1, 600)
        self.sec.setValue(int(cfg.get("movetime_sec", 5)))
        form.addRow("思考時間(秒)", self.sec)
        self.gsec = QDoubleSpinBox()
        self.gsec.setRange(0.1, 120)
        self.gsec.setSingleStep(0.1)
        self.gsec.setDecimals(1)
        self.gsec.setValue(float(cfg.get("game_sec", 1)))
        self.gsec.setToolTip("「全局を解析」で、1手(1局面)あたりに考えさせる秒数")
        form.addRow("全局解析：1手あたり(秒)", self.gsec)
        self.gwork = QSpinBox()
        self.gwork.setRange(1, 8)
        self.gwork.setValue(int(cfg.get("game_workers", 3)))
        self.gwork.setToolTip("同時に動かすエンジンの数。パソコンのコア数より多くしないでください（4コアなら3〜4）")
        form.addRow("全局解析：並列エンジン数", self.gwork)
        self.msec = QSpinBox()
        self.msec.setRange(0, 86400)
        self.msec.setSpecialValueText("無制限")
        self.msec.setValue(int(cfg.get("mate_sec", 60)))
        self.msec.setToolTip("「詰みを探す」の制限時間(秒)。0=無制限（停止ボタンで止める）")
        form.addRow("詰み探索の時間(秒)", self.msec)
        self.mpv = QSpinBox()
        self.mpv.setRange(1, 10)
        self.mpv.setValue(int(cfg.get("multipv", 3)))
        form.addRow("候補手の数", self.mpv)

        self.provider = QComboBox()
        self.provider.addItem("Gemini（Google）", "gemini")
        self.provider.addItem("ChatGPT（OpenAI）", "openai")
        self.provider.addItem("Claude（Anthropic）", "claude")
        self.provider.addItem("Qwen（アリババ）", "qwen")
        self.provider.addItem("Groq（高速・無料枠あり）", "groq")
        self.provider.addItem("Mistral（無料枠あり）", "mistral")
        self.provider.addItem("OpenRouter（無料モデルあり）", "openrouter")
        self.provider.setCurrentIndex(max(0, self.provider.findData(cfg.get("llm_provider", "gemini"))))
        form.addRow("解説に使うAI", self.provider)
        self.key = QLineEdit(cfg.get("gemini_api_key", ""))
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Gemini APIキー", self.key)
        self.model = QLineEdit(cfg.get("gemini_model", "gemini-2.5-flash"))
        form.addRow("Geminiモデル名", self.model)
        self.okey = QLineEdit(cfg.get("openai_api_key", ""))
        self.okey.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("OpenAI APIキー", self.okey)
        self.omodel = QComboBox()
        self.omodel.setEditable(True)            # 一覧に無い名前も直接入力できる
        self.omodel.addItems(core.OPENAI_MODELS)
        self.omodel.setCurrentText(cfg.get("openai_model", "gpt-5.4-mini"))
        self.omodel.setToolTip("gpt-5.4-mini = GPT-5.4 Mini（標準）。一覧に無いモデルは、名前を直接入力してください")
        form.addRow("OpenAIモデル名", self.omodel)
        self.akey = QLineEdit(cfg.get("anthropic_api_key", ""))
        self.akey.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Anthropic APIキー", self.akey)
        self.amodel = QComboBox()
        self.amodel.setEditable(True)
        self.amodel.addItems(core.CLAUDE_MODELS)
        self.amodel.setCurrentText(cfg.get("anthropic_model", "claude-sonnet-5-5"))
        form.addRow("Claudeモデル名", self.amodel)
        self.qkey = QLineEdit(cfg.get("qwen_api_key", ""))
        self.qkey.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Qwen APIキー", self.qkey)
        self.qmodel = QComboBox()
        self.qmodel.setEditable(True)
        self.qmodel.addItems(core.QWEN_MODELS)
        self.qmodel.setCurrentText(cfg.get("qwen_model", "qwen3.8-max"))
        form.addRow("Qwenモデル名", self.qmodel)
        self.qurl = QLineEdit(cfg.get("qwen_base_url") or core.DEFAULT_CONFIG["qwen_base_url"])
        self.qurl.setToolTip("QwenのOpenAI互換URL（…/compatible-mode/v1 まで）")
        form.addRow("Qwen接続先URL", self.qurl)
        self.gkey = QLineEdit(cfg.get("groq_api_key", ""))
        self.gkey.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Groq APIキー", self.gkey)
        self.gmodel = QComboBox()
        self.gmodel.setEditable(True)
        self.gmodel.addItems(core.GROQ_MODELS)
        self.gmodel.setCurrentText(cfg.get("groq_model", "openai/gpt-oss-120b"))
        form.addRow("Groqモデル名", self.gmodel)
        self.mkey = QLineEdit(cfg.get("mistral_api_key", ""))
        self.mkey.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Mistral APIキー", self.mkey)
        self.mmodel = QComboBox()
        self.mmodel.setEditable(True)
        self.mmodel.addItems(core.MISTRAL_MODELS)
        self.mmodel.setCurrentText(cfg.get("mistral_model", "open-mistral-nemo"))
        form.addRow("Mistralモデル名", self.mmodel)
        self.rkey = QLineEdit(cfg.get("openrouter_api_key", ""))
        self.rkey.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("OpenRouter APIキー", self.rkey)
        self.rmodel = QComboBox()
        self.rmodel.setEditable(True)
        self.rmodel.addItems(core.OPENROUTER_MODELS)
        self.rmodel.setCurrentText(cfg.get("openrouter_model") or core.OPENROUTER_MODELS[0])
        self.rmodel.setToolTip("末尾が :free のモデルは無料。先頭が混雑で失敗すると、別の無料モデルを自動で試します")
        form.addRow("OpenRouterモデル名", self.rmodel)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    @staticmethod
    def _row(edit, btn):
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(edit, 1)
        lay.addWidget(btn)
        return w

    def _pick_engine(self):
        path, _ = QFileDialog.getOpenFileName(self, "エンジンを選ぶ", "", "実行ファイル (*.exe);;すべて (*)")
        if path:
            self.engine.setText(path)

    def _pick_dir(self):
        path = QFileDialog.getExistingDirectory(self, "評価関数のフォルダを選ぶ")
        if path:
            self.evaldir.setText(path)

    def apply_to(self, cfg):
        opts = {"Threads": self.threads.value(), "USI_Hash": self.hash.value()}
        if self.evaldir.text().strip():
            opts["EvalDir"] = self.evaldir.text().strip()
        for line in self.extra.toPlainText().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k.strip():
                    opts[k.strip()] = v.strip()
        cfg.update({"engine_path": self.engine.text().strip(), "engine_options": opts,
                    "movetime_sec": self.sec.value(), "mate_sec": self.msec.value(), "game_sec": self.gsec.value(), "game_workers": self.gwork.value(), "multipv": self.mpv.value(),
                    "gemini_api_key": self.key.text().strip(), "gemini_model": self.model.text().strip(),
                    "llm_provider": self.provider.currentData(),
                    "openai_api_key": self.okey.text().strip(), "openai_model": self.omodel.currentText().strip(),
                    "anthropic_api_key": self.akey.text().strip(), "anthropic_model": self.amodel.currentText().strip(),
                    "qwen_api_key": self.qkey.text().strip(), "qwen_model": self.qmodel.currentText().strip(),
                    "qwen_base_url": self.qurl.text().strip(),
                    "groq_api_key": self.gkey.text().strip(), "groq_model": self.gmodel.currentText().strip(),
                    "mistral_api_key": self.mkey.text().strip(), "mistral_model": self.mmodel.currentText().strip(),
                    "openrouter_api_key": self.rkey.text().strip(), "openrouter_model": self.rmodel.currentText().strip()})


# ====================================================================== メイン画面
class Sig(QObject):
    cands = pyqtSignal(int, list, bool)
    error = pyqtSignal(int, str)
    gemini = pyqtSignal(int, str)
    gemini_err = pyqtSignal(int, str)
    shot = pyqtSignal(object)
    shot_err = pyqtSignal(str)
    mate = pyqtSignal(object)
    game_prog = pyqtSignal(str, int, object)
    game_done = pyqtSignal(str, bool)
    game_err = pyqtSignal(str)
    review = pyqtSignal(str, str)
    review_err = pyqtSignal(str)


class EvalGraph(QWidget):
    """一局の評価値グラフ。上＝先手有利、下＝後手有利。クリックでその手数へ移動。"""
    plotClicked = pyqtSignal(int)
    CLIP = 2000

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(130)
        self.setMaximumHeight(170)
        self.setMouseTracking(True)
        self.data = None            # 各手数の記録(dict)またはNone(未解析)のリスト
        self.marks = {}             # 手数 -> {"grade":..}
        self.cursor = 0
        self.tipfn = None
        self.msg = "「全局を解析」を押すと、一局の評価値グラフが出ます"

    def set_data(self, data, marks=None):
        self.data, self.marks = data, (marks or {})
        self.update()

    def set_cursor(self, ply):
        self.cursor = ply
        self.update()

    def _geom(self):
        m = 8
        return m, m + 4, self.width() - 2 * m, self.height() - 2 * m - 8

    def _x(self, i, n, left, w):
        return left + (w * i / n if n else 0)

    def _y(self, v, top, h):
        v = max(-self.CLIP, min(self.CLIP, v))
        return top + h / 2 - v / self.CLIP * (h / 2)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(250, 250, 250))
        left, top, w, h = self._geom()
        p.setPen(QPen(QColor(200, 200, 200), 1))
        p.drawRect(left, top, w, h)
        for frac in (0.25, 0.75):
            p.drawLine(int(left), int(top + h * frac), int(left + w), int(top + h * frac))
        p.setPen(QPen(QColor(120, 120, 120), 1))
        p.drawLine(int(left), int(top + h / 2), int(left + w), int(top + h / 2))
        f = QFont(); f.setPointSize(8); p.setFont(f)
        p.setPen(QColor(120, 120, 120))
        p.drawText(int(left + 3), int(top + 12), "先手有利")
        p.drawText(int(left + 3), int(top + h - 4), "後手有利")
        d = self.data
        if not d or len(d) < 2:
            p.setPen(QColor(110, 110, 110))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.msg)
            p.end()
            return
        n = len(d) - 1
        pts = [(i, e["v"]) for i, e in enumerate(d) if e is not None]
        zero = top + h / 2
        # 面（上は先手側＝青、下は後手側＝赤）
        for up, col in ((True, QColor(90, 140, 220, 90)), (False, QColor(230, 110, 100, 90))):
            p.save()
            p.setClipRect(QRectF(left, top if up else zero, w, h / 2))
            poly = QPolygonF()
            if pts:
                poly.append(QPointF(self._x(pts[0][0], n, left, w), zero))
                for i, v in pts:
                    poly.append(QPointF(self._x(i, n, left, w), self._y(v, top, h)))
                poly.append(QPointF(self._x(pts[-1][0], n, left, w), zero))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawPolygon(poly)
            p.restore()
        p.setPen(QPen(QColor(40, 60, 120), 1.6))
        for (i0, v0), (i1, v1) in zip(pts, pts[1:]):
            if i1 == i0 + 1:
                p.drawLine(QPointF(self._x(i0, n, left, w), self._y(v0, top, h)),
                           QPointF(self._x(i1, n, left, w), self._y(v1, top, h)))
        cols = {"??": QColor(220, 30, 30), "?": QColor(240, 130, 20), "?!": QColor(220, 190, 0)}
        for i, m in self.marks.items():
            if i < len(d) and d[i] is not None:
                p.setBrush(cols.get(m["grade"], QColor(200, 0, 0)))
                p.setPen(QPen(QColor(60, 60, 60), 1))
                p.drawEllipse(QPointF(self._x(i, n, left, w), self._y(d[i]["v"], top, h)), 4, 4)
        cx = self._x(min(self.cursor, n), n, left, w)
        p.setPen(QPen(QColor(210, 40, 40), 1.4))
        p.drawLine(QPointF(cx, top), QPointF(cx, top + h))
        p.end()

    def _ply_at(self, x):
        if not self.data or len(self.data) < 2:
            return None
        left, top, w, h = self._geom()
        n = len(self.data) - 1
        return max(0, min(n, round((x - left) / w * n))) if w > 0 else None

    def mousePressEvent(self, ev):
        i = self._ply_at(ev.position().x())
        if i is not None:
            self.plotClicked.emit(i)

    def mouseMoveEvent(self, ev):
        i = self._ply_at(ev.position().x())
        if i is not None and self.tipfn:
            QToolTip.showText(ev.globalPosition().toPoint(), self.tipfn(i), self)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("将棋 検討ツール")
        self.resize(1440, 780)
        self.cfg = core.load_config(CONFIG_PATH)
        self.engine = None
        self.engine_lock = threading.Lock()
        self.gen = 0
        self.analyzing = False
        self.cands = []
        self.cands_final = False
        self.explain_pending = False
        self.names = ["先手", "後手"]
        self.start_sfen = shogi.STARTING_SFEN
        self.moves = []
        self.cursor = 0
        self.cur_board = shogi.Board()
        self.sig = Sig()
        self.sig.cands.connect(self.on_cands)
        self.sig.error.connect(self.on_error)
        self.sig.gemini.connect(self.on_gemini)
        self.sig.mate.connect(self.on_mate)
        self.sig.game_prog.connect(self.on_game_prog)
        self.sig.game_done.connect(self.on_game_done)
        self.sig.game_err.connect(self.on_game_err)
        self.sig.review.connect(self.on_review)
        self.sig.review_err.connect(self.on_review_err)
        self.game_running = False
        self.game_cancel = threading.Event()
        self.eval_key = None
        self.evals = None
        self.swings = {}
        self.sig.gemini_err.connect(self.on_gemini_err)

        # 左：盤面と操作ボタン
        self.boardw = BoardWidget()
        self.boardw.moveMade.connect(self.on_user_move)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.addWidget(self.boardw, 1)
        self.graph = EvalGraph()
        self.graph.plotClicked.connect(self.go_to_ply)
        self.graph.tipfn = self.graph_tip
        lv.addWidget(self.graph)
        nav3 = QHBoxLayout()
        self.btn_game = QPushButton("全局を解析（評価値グラフ）")
        self.btn_game.setToolTip("読み込んだ棋譜の全局面をエンジンで解析し、評価値のグラフを出します。もう一度押すと中止します")
        self.btn_game.clicked.connect(self.toggle_game_analysis)
        self.game_label = QLabel("")
        self.btn_open_eval = QPushButton("評価グラフを開く…")
        self.btn_open_eval.setToolTip("保存した評価グラフ（解説保存\\評価グラフ フォルダのJSON）を選んで開きます")
        self.btn_open_eval.clicked.connect(self.open_eval_file)
        nav3.addWidget(self.btn_game)
        nav3.addWidget(self.btn_open_eval)
        self.btn_review = QPushButton("全局をAIで講評")
        self.btn_review.setToolTip("全局解析の結果（評価値の流れと勝負所）を、設定で選んだAIに渡して、一局の講評を書いてもらいます")
        self.btn_review.clicked.connect(self.review_game)
        nav3.addWidget(self.btn_review)
        nav3.addWidget(self.game_label, 1)
        lv.addLayout(nav3)
        nav = QHBoxLayout()
        for text, fn in [("|◀ 最初", self.go_first), ("◀ 1手戻る", self.go_prev), ("1手進む ▶", self.go_next),
                         ("最後 ▶|", self.go_last), ("盤面を反転", self.flip),
                         ("平手に戻す", self.reset_board), ("SFEN貼付", self.paste_sfen),
                         ("KIF読込", self.open_kif), ("KIF貼付", self.paste_kif), ("KIF保存", self.save_kif),
                         ("SFENコピー", self.copy_sfen)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            nav.addWidget(b)
        lv.addLayout(nav)
        nav2 = QHBoxLayout()
        self.btn_shot = QPushButton("スクショ→局面（クリップボードの画像）")
        self.btn_shot.clicked.connect(lambda: self.import_screenshot())
        self.btn_cap = QPushButton("画面を撮影→局面")
        self.btn_cap.setToolTip("このウィンドウを隠してから画面全体を撮影し、読み取ります（棋桜を全画面にしておく）")
        self.btn_cap.clicked.connect(self.capture_screen)
        self.btn_edit = QPushButton("編集モード")
        self.btn_edit.setCheckable(True)
        self.btn_edit.toggled.connect(self.toggle_edit)
        btn_turn = QPushButton("手番切替")
        btn_turn.clicked.connect(self.toggle_turn)
        self.btn_mate = QPushButton("詰みを探す")
        self.btn_mate.setToolTip("詰将棋用：この局面の詰み手順をエンジンの詰み探索(go mate)で探します。止めるには右の「停止」")
        self.btn_mate.clicked.connect(self.search_mate)
        for b in (self.btn_shot, self.btn_cap, self.btn_edit, btn_turn, self.btn_mate):
            nav2.addWidget(b)
        lv.addLayout(nav2)
        self.boardw.positionEdited.connect(self.on_position_edited)
        self.sig.shot.connect(self.on_shot)
        self.sig.shot_err.connect(self.on_shot_err)

        # 右：検討結果と解説
        right = QWidget()
        right.setMinimumWidth(440)
        rv = QVBoxLayout(right)
        self.info = QLabel()
        self.info.setStyleSheet("font-weight:bold; font-size:14px;")
        rv.addWidget(self.info)

        # 棋譜に付いていたコメント（KIFの「*」の行）。コメントのある手でだけ出る
        self.kif_comments, self.kif_comment_sfen, self.kif_comment_moves = {}, None, []
        self.comment_box = QTextBrowser()
        self.comment_box.setOpenExternalLinks(True)
        self.comment_box.setMaximumHeight(190)
        self.comment_box.setStyleSheet("background:#fffbe8;")
        self.comment_box.hide()
        rv.addWidget(self.comment_box)

        row = QHBoxLayout()
        self.btn_go = QPushButton("検討開始")
        self.btn_go.clicked.connect(lambda: self.request_analysis())
        self.btn_stop = QPushButton("停止")
        self.btn_stop.clicked.connect(self.stop_analysis)
        self.auto = QCheckBox("自動で検討")
        self.auto.setChecked(True)
        row.addWidget(self.btn_go)
        row.addWidget(self.btn_stop)
        row.addWidget(self.auto, 1)
        btn_set = QPushButton("設定…")
        btn_set.clicked.connect(self.open_settings)
        row.addWidget(btn_set)
        rv.addLayout(row)

        rv.addWidget(QLabel("エンジンの候補手（クリックで盤面に矢印）"))
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setMaximumHeight(210)
        self.list.currentRowChanged.connect(self.on_pick_cand)
        rv.addWidget(self.list)

        row2 = QHBoxLayout()
        self.btn_explain = QPushButton("この局面を解説する")
        self.btn_explain.clicked.connect(self.explain)
        self.btn_copy = QPushButton("文章をコピー")
        self.btn_copy.clicked.connect(self.copy_prompt)
        self.btn_open_saved = QPushButton("保存した解説を開く…")
        self.btn_open_saved.clicked.connect(self.open_saved)
        row2.addWidget(self.btn_explain, 1)
        row2.addWidget(self.btn_copy)
        row2.addWidget(self.btn_open_saved)
        rv.addLayout(row2)

        self.result = QTextEdit()
        self.result.setReadOnly(True)
        self.result.setPlaceholderText("ここに解説が出ます")
        self.update_ai_labels()
        rv.addWidget(self.result, 1)
        self.status = QLabel("盤面の駒をクリックして動かせます（右クリックで選択解除）")
        self.status.setWordWrap(True)
        rv.addWidget(self.status)

        # 盤の左：棋譜（指し手の一覧）。クリックでその局面へ
        movepanel = QWidget()
        movepanel.setMinimumWidth(220)
        mv = QVBoxLayout(movepanel)
        mv.setContentsMargins(4, 4, 0, 4)
        self.move_title = QLabel("棋譜")
        self.move_title.setStyleSheet("font-weight:bold;")
        mv.addWidget(self.move_title)
        self.move_list = QListWidget()
        self.move_list.setStyleSheet("QListWidget{font-size:14px;} QListWidget::item{padding:1px 2px;}")
        self.move_list.currentRowChanged.connect(self.on_move_row)
        mv.addWidget(self.move_list, 1)
        self._ml_key = None

        sp = QSplitter()
        sp.addWidget(movepanel)
        sp.addWidget(left)
        sp.addWidget(right)
        sp.setStretchFactor(0, 0)
        sp.setStretchFactor(1, 3)
        sp.setStretchFactor(2, 2)
        sp.setSizes([265, 650, 480])
        self.setCentralWidget(sp)

        QShortcut(QKeySequence("Left"), self, self.go_prev)
        QShortcut(QKeySequence("Right"), self, self.go_next)
        self.refresh(analyze=False)

    # ---------------------------------------------------------- 局面の管理
    def rebuild_board(self):
        return core.board_from(self.start_sfen, self.moves[:self.cursor])

    def refresh(self, analyze=True):
        self.gen += 1
        if self.analyzing:
            self.stop_analysis()
        self.analyzing = False
        self.cands, self.cands_final, self.explain_pending = [], False, False
        self.cur_board = self.rebuild_board()
        last = self.moves[self.cursor - 1] if self.cursor > 0 else None
        self.boardw.names = list(self.names)
        shown = [n for n in self.names if n not in ("先手", "後手")]
        self.setWindowTitle("将棋 検討ツール" + (f"　▲{self.names[0]} 対 △{self.names[1]}" if shown else ""))
        self.boardw.set_position(self.cur_board, last)
        self.list.clear()
        self.result.clear()
        turn = "先手" if self.cur_board.turn == shogi.BLACK else "後手"
        text = f"{self.cursor}手目まで　{turn}の手番"
        if safe(self.cur_board.is_checkmate):
            text += "　詰み"
        elif safe(self.cur_board.is_check):
            text += "　王手"
        self.info.setText(text)
        self.show_kif_comment()
        self.sync_eval_graph()
        self.refresh_move_list()
        if analyze and self.auto.isChecked() and not self.game_running and not safe(self.cur_board.is_checkmate):
            self.request_analysis()

    def on_move_row(self, row):
        if row >= 0 and row != self.cursor and row <= len(self.moves):
            self.go_to_ply(row)

    def refresh_move_list(self):
        """盤の左の棋譜欄を作り直し（必要なときだけ）、今の手の行を選ぶ。"""
        try:
            ev = self.evals if (self.evals and len(self.evals) == len(self.moves) + 1) else None
            sw = self.swings if ev else {}
            cms = self.current_kif_comments() if self.kif_comments else {}
            key = (self.start_sfen, tuple(self.moves), sum(1 for e in (ev or []) if e), len(sw or {}), tuple(sorted(cms)))
            if key != self._ml_key:
                self._ml_key = key
                import re as _re
                from PyQt6.QtGui import QBrush
                from PyQt6.QtWidgets import QListWidgetItem
                n = len(self.moves)
                self.move_list.blockSignals(True)
                self.move_list.clear()
                first = "開始局面" + (" 💬" if 0 in cms else "")
                self.move_list.addItem(first)
                b = shogi.Board(self.start_sfen)
                prev = None
                black_first = (b.turn == shogi.BLACK)
                for i, usi in enumerate(self.moves, 1):
                    txt, prev = core._kif_move(b, usi, prev)
                    txt = _re.sub(r"\(\d\d\)$", "", txt)
                    side = "▲" if b.turn == shogi.BLACK else "△"
                    line = f"{i:>3} {side}{txt}"
                    e = ev[i] if ev else None
                    if e:
                        line += "  " + ("詰" if (e.get("mate") is not None or abs(e["v"]) >= core.MATE_VALUE - 1000)
                                        else f"{e['v']:+d}")
                    s = sw.get(i) if sw else None
                    if s:
                        line += " " + s["grade"]
                    if i in cms or (i == n and any(k > n for k in cms)):
                        line += " 💬"
                    it = QListWidgetItem(line)
                    if s:
                        col = {"??": "#c01818", "?": "#d0601a", "?!": "#8a6a10"}.get(s["grade"])
                        if col:
                            it.setForeground(QBrush(QColor(col)))
                    self.move_list.addItem(it)
                    b.push(shogi.Move.from_usi(usi))
                self.move_list.blockSignals(False)
                nm = [x for x in self.names if x not in ("先手", "後手")]
                self.move_title.setText("棋譜　" + (f"{n}手" if n else "（指し手なし）"))
            self.move_list.blockSignals(True)
            self.move_list.setCurrentRow(min(self.cursor, self.move_list.count() - 1))
            self.move_list.blockSignals(False)
            self.move_list.scrollToItem(self.move_list.currentItem(), QListWidget.ScrollHint.PositionAtCenter)
        except Exception:
            pass

    def kif_comment_here(self):
        """今の局面に付いている棋譜コメント（なければ空）。指し手が棋譜と違っていたら空。"""
        try:
            c = self.cursor
            same = (self.kif_comments and self.start_sfen == self.kif_comment_sfen
                    and self.moves[:c] == self.kif_comment_moves[:c])
            if not same:
                return ""
            parts = []
            if c in self.kif_comments:
                parts.append(self.kif_comments[c])
            if c == len(self.kif_comment_moves) and len(self.moves) == c:
                # 「投了」など、指し手の後ろに付いたコメントも最後の局面で一緒に出す
                for k in sorted(self.kif_comments):
                    if k > c:
                        parts.append(self.kif_comments[k])
            return "\n\n".join(parts)
        except Exception:
            return ""

    def show_kif_comment(self):
        """今の手に付いている棋譜コメントを出す。指し手が棋譜と違っていたら出さない。"""
        text = self.kif_comment_here()
        if not text.strip():
            self.comment_box.hide()
            self.comment_box.clear()
            return
        import html as _html
        import re as _re
        esc = _html.escape(text).replace("\n", "<br>")
        esc = _re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', esc)
        head = "対局前のコメント" if self.cursor == 0 else f"{self.cursor}手目のコメント"
        self.comment_box.setHtml(f"<b>💬 {head}</b><br>{esc}")
        self.comment_box.show()

    def on_user_move(self, usi):
        self.moves = self.moves[:self.cursor] + [usi]
        self.cursor += 1
        self.refresh()

    def go_to_ply(self, ply):
        self.cursor = max(0, min(len(self.moves), int(ply)))
        self.refresh()

    def go_first(self):
        self.cursor = 0
        self.refresh()

    def go_prev(self):
        if self.cursor > 0:
            self.cursor -= 1
            self.refresh()

    def go_next(self):
        if self.cursor < len(self.moves):
            self.cursor += 1
            self.refresh()

    def go_last(self):
        self.cursor = len(self.moves)
        self.refresh()

    def flip(self):
        self.boardw.flipped = not self.boardw.flipped
        self.boardw.update()

    def reset_board(self):
        self.start_sfen, self.moves, self.cursor = shogi.STARTING_SFEN, [], 0
        self.names = ["先手", "後手"]
        self.refresh()

    def paste_sfen(self):
        text, ok = QInputDialog.getText(self, "SFEN貼り付け", "SFENを貼り付けてください：")
        if not ok or not text.strip():
            return
        s = text.strip()
        for prefix in ("position sfen ", "sfen "):
            if s.startswith(prefix):
                s = s[len(prefix):]
        s = s.split(" moves ")[0].strip()
        try:
            shogi.Board(s)
        except Exception:
            QMessageBox.warning(self, "SFENを読めません", "SFENの形式を確認してください。")
            return
        self.start_sfen, self.moves, self.cursor = s, [], 0
        self.refresh()

    def copy_sfen(self):
        QApplication.clipboard().setText(self.cur_board.sfen())
        self.status.setText("この局面のSFENをコピーしました")

    def open_kif(self):
        path, _ = QFileDialog.getOpenFileName(self, "棋譜を選ぶ", "", "KIF棋譜 (*.kif *.kifu);;すべて (*)")
        if not path:
            return
        try:
            game = core.load_kif(path)
            core.board_from(game["sfen"], game["moves"])
        except Exception as e:
            QMessageBox.warning(self, "棋譜を読めません", str(e))
            return
        self._apply_game(game)

    def current_kif_comments(self):
        """読み込んだ棋譜のコメントのうち、今の指し手の並びに合うもの。"""
        if not self.kif_comments or self.start_sfen != self.kif_comment_sfen:
            return {}
        n = 0
        for a, b in zip(self.moves, self.kif_comment_moves):
            if a != b:
                break
            n += 1
        out = {k: v for k, v in self.kif_comments.items() if k <= n}
        if n == len(self.moves) == len(self.kif_comment_moves):
            out.update({k: v for k, v in self.kif_comments.items() if k > n})
        return out

    def save_kif(self):
        """今の棋譜をKIFで保存する。全局解析の結果があれば、各手のコメントに評価値と読み筋（候補1）を入れる。"""
        import re as _re
        from datetime import datetime
        if not self.moves and shogi.Board(self.start_sfen).sfen() == shogi.STARTING_SFEN:
            QMessageBox.information(self, "案内", "保存する棋譜がありません。")
            return
        evals = self.evals if (self.evals and len(self.evals) == len(self.moves) + 1
                               and any(e is not None for e in self.evals)) else None
        base = f"{datetime.now():%Y%m%d}-{self.names[0]}-{self.names[1]}"
        base = _re.sub(r'[\\/:*?"<>|\s]+', "_", base)
        start = os.path.join(self.save_dir(), base + ".kif")
        path, filt = QFileDialog.getSaveFileName(
            self, "KIFで保存", start, "KIF（Shift-JIS・ふつうの棋譜ソフト向け）(*.kif);;KIFU（UTF-8）(*.kifu)")
        if not path:
            return
        utf8 = path.lower().endswith(".kifu") or ("KIFU" in (filt or "") and not path.lower().endswith(".kif"))
        if not _re.search(r"\.kifu?$", path, _re.I):
            path += ".kifu" if utf8 else ".kif"
        try:
            text = core.export_kif(self.start_sfen, self.moves, self.names, self.current_kif_comments(),
                                   evals, self.swings if evals else None)
            with open(path, "wb") as f:
                f.write(core.kif_bytes(text, utf8))
        except Exception as e:
            QMessageBox.warning(self, "保存できません", str(e))
            return
        what = "評価値と読み筋つき" if evals else "解析結果なし（先に「全局を解析」をすると評価値と読み筋が入ります）"
        self.status.setText(f"KIFを保存しました（{len(self.moves)}手・{what}）：{path}")

    def paste_kif(self):
        """クリップボードのKIF（指し手つき、または盤面の図だけ）を読み込む。"""
        text = QApplication.clipboard().text()
        if not text.strip():
            QMessageBox.information(self, "案内", "クリップボードに文字がありません。KIFをコピーしてから押してください。")
            return
        try:
            game = core.load_kif_text(text)
            core.board_from(game["sfen"], game["moves"])
        except Exception as e:
            QMessageBox.warning(self, "棋譜を読めません", str(e))
            return
        self._apply_game(game, from_clip=True)

    def _apply_game(self, game, from_clip=False):
        self.start_sfen, self.moves, self.names = game["sfen"], game["moves"], game["names"]
        self.kif_comments = dict(game.get("comments") or {})
        self.kif_comment_sfen, self.kif_comment_moves = game["sfen"], list(game["moves"])
        self.cursor = 0 if (from_clip and not self.moves) or self._is_problem(game) else len(self.moves)
        self.refresh()
        src = "クリップボード" if from_clip else "棋譜"
        if self.moves:
            extra = f"　コメント{len(self.kif_comments)}か所あり" if self.kif_comments else ""
            self.status.setText(f"{len(self.moves)}手の{src}を読み込みました（◀▶キーで前後に動かせます）{extra}")
        else:
            self.status.setText(f"{src}から局面だけ読み込みました（指し手はありません）")

    @staticmethod
    def _is_problem(game):
        """詰将棋の棋譜（先手玉が無い初形）は、最初の局面を見せる。"""
        try:
            return shogi.Board(game["sfen"]).king_squares[shogi.BLACK] is None
        except Exception:
            return False

    # ---------------------------------------------------------- スクショ取り込み・編集
    def capture_screen(self):
        """このウィンドウを隠し、少し待ってから画面全体を撮影して読み取る。"""
        if self.cfg.get("screenshot_mode") == "gemini" and not self.cfg.get("gemini_api_key"):
            QMessageBox.information(self, "案内", "Gemini APIキーが未設定です。「設定…」で入力してください。")
            return
        screens = QApplication.screens()
        idx = self.cfg.get("capture_screen")
        if isinstance(idx, int) and 0 <= idx < len(screens):
            targets = [screens[idx]]
        elif self.cfg.get("screenshot_mode") == "gemini":
            targets = [self.screen() or screens[0]]
        else:
            targets = list(screens)          # 全モニターを撮って、棋桜が写っているものを自動で選ぶ
        delay = int(self.cfg.get("capture_delay_ms", 1500))
        self.showMinimized()

        def shoot():
            pngs = [p for p in (screen_png(s, 1800 if self.cfg.get("screenshot_mode") == "gemini" else None)
                                for s in targets) if p]
            self.showNormal()
            self.activateWindow()
            if not pngs:
                QMessageBox.warning(self, "撮影できません", "画面を撮影できませんでした。")
                return
            self.import_screenshot(pngs)

        QTimer.singleShot(delay, shoot)

    def import_screenshot(self, png=None):
        if self.cfg.get("screenshot_mode") == "gemini" and not self.cfg.get("gemini_api_key"):
            QMessageBox.information(self, "案内", "Gemini APIキーが未設定です。「設定…」で入力してください。")
            return
        if not png:
            png = clipboard_png(1800 if self.cfg.get("screenshot_mode") == "gemini" else 100000)
        pngs = png if isinstance(png, list) else [png]      # 複数のモニターの画像が来たら、読めたものを使う
        if pngs[0] is None:
            QMessageBox.information(
                self, "案内", "クリップボードに画像がありません。\nWindowsなら「Win＋Shift＋S」で盤面の範囲を選ぶと、"
                "画像がコピーされます。そのあと、もう一度このボタンを押してください。")
            return
        self.btn_shot.setEnabled(False)
        self.status.setText("スクショを読み取り中…")
        cfg = dict(self.cfg)

        def work():
            first_err = None
            for p in pngs:
                try:
                    self.sig.shot.emit(core.read_screenshot(p, cfg))
                    return
                except Exception as e:
                    first_err = first_err or str(e)
            self.sig.shot_err.emit(first_err or "読み取れませんでした")

        threading.Thread(target=work, daemon=True).start()

    def on_shot(self, result):
        self.btn_shot.setEnabled(True)
        self.start_sfen, self.moves, self.cursor, self.names = result["sfen"], [], 0, ["先手", "後手"]
        self.refresh(analyze=not self.btn_edit.isChecked())
        lines = ["・" + t for _, t in result["messages"]]
        if any(lv in ("error", "warn") for lv, _ in result["messages"]):
            QMessageBox.warning(self, "読み取り結果の確認",
                                "局面を読み込みました。次の点を確認してください。\n\n" + "\n".join(lines)
                                + "\n\n間違いがあれば「編集モード」で直せます。")
        self.status.setText("読み取り完了：" + (lines[-1][1:] if lines else ""))

    def on_shot_err(self, msg):
        self.btn_shot.setEnabled(True)
        self.status.setText("読み取りに失敗：" + msg)
        QMessageBox.warning(self, "スクショを読み取れません", msg)

    def toggle_edit(self, checked):
        self.boardw.edit_mode = checked
        self.boardw.update()
        if checked:
            self.status.setText("編集モード：マスをクリックして駒を置く／消す。持ち駒は点線の帯をクリック")
        else:
            self.refresh()

    def on_position_edited(self, sfen):
        try:
            shogi.Board(sfen)
        except Exception:
            return
        self.start_sfen, self.moves, self.cursor, self.names = sfen, [], 0, ["先手", "後手"]
        self.refresh(analyze=not self.btn_edit.isChecked())

    def toggle_turn(self):
        cells, hands, turn = core.board_to_model(self.cur_board)
        self.start_sfen, self.moves, self.cursor = core.model_to_sfen(cells, hands, 1 - turn), [], 0
        self.refresh(analyze=not self.btn_edit.isChecked())

    # ---------------------------------------------------------- エンジン検討
    def _get_engine(self):
        if self.engine is None:
            path = self.cfg.get("engine_path", "")
            if not path:
                raise RuntimeError("エンジンが登録されていません。右上の「設定…」から登録してください。")
            eng = core.UsiEngine(path, self.cfg.get("engine_options", {}))
            eng.start()
            self.engine = eng
        return self.engine

    def close_engine(self):
        eng, self.engine = self.engine, None
        if eng:
            eng.stop()
            eng.close()

    def request_analysis(self, explain_after=False):
        if safe(self.cur_board.is_checkmate):
            self.status.setText("詰みの局面です")
            return
        if self.analyzing and self.engine:
            self.engine.stop()
        self.gen += 1
        gen = self.gen
        self.analyzing = True
        self.cands_final = False
        self.explain_pending = explain_after
        sfen = self.cur_board.sfen()
        ms, mpv = int(self.cfg["movetime_sec"]) * 1000, int(self.cfg["multipv"])
        self.status.setText("エンジンで検討中…（初回はエンジンの起動に時間がかかります）")

        def work():
            try:
                with self.engine_lock:
                    if gen != self.gen:
                        return
                    eng = self._get_engine()
                    cands, _ = eng.analyze(sfen, ms, mpv, on_info=lambda c: self.sig.cands.emit(gen, c, False))
                self.sig.cands.emit(gen, cands, True)
            except Exception as e:
                self.sig.error.emit(gen, str(e))

        threading.Thread(target=work, daemon=True).start()

    # ---------------------------------------------------------- 詰み探索（詰将棋）
    def search_mate(self):
        if safe(self.cur_board.is_checkmate):
            self.status.setText("すでに詰みの局面です")
            return
        if self.analyzing and self.engine:
            self.engine.stop()
        self.gen += 1                      # 動いている通常の検討の結果は捨てる
        self.analyzing = False
        sfen = self.cur_board.sfen()
        ms = int(self.cfg.get("mate_sec", 60)) * 1000
        board = shogi.Board(sfen)
        compare = self.moves if (self.cursor == 0 and self.moves and shogi.Board(self.start_sfen).sfen() == sfen) else None
        self.btn_mate.setEnabled(False)
        self.status.setText("詰み探索中…（右の「停止」で中止。初回はエンジンの起動に時間がかかります）")

        def work():
            t0 = time.time()
            try:
                with self.engine_lock:
                    eng = self._get_engine()
                    runs = []
                    for s, kname in core.mate_king_candidates(sfen, 2):
                        state, pv, n = eng.mate(s, ms)
                        runs.append({"king": kname, "state": state, "pv": pv, "n": n})
                        if state != "mate":
                            break
                self.sig.mate.emit({"state": runs[0]["state"], "runs": runs, "sec": time.time() - t0,
                                    "sfen": sfen, "compare": compare})
            except Exception as e:
                self.sig.mate.emit({"state": "error", "msg": str(e), "runs": [], "sec": time.time() - t0})

        threading.Thread(target=work, daemon=True).start()

    def on_mate(self, r):
        self.btn_mate.setEnabled(True)
        st, sec = r["state"], r["sec"]
        if st == "error":
            self.status.setText("エラー：" + r["msg"])
            QMessageBox.warning(self, "エンジンのエラー", r["msg"])
            return
        runs = r["runs"]
        first = runs[0]
        if st == "mate":
            pv = first["pv"]
            b = shogi.Board(r["sfen"])
            n = len(pv)
            text = [f"## 詰みが見つかりました：{n}手詰（{sec:.1f}秒）", ""]
            if first["n"] and first["n"] != n:
                text += [f"（エンジンは詰みまで{first['n']}手と報告。表示できた手順は{n}手分です）", ""]
            text += ["エンジンの手順（最短とは限りません）：", "", core.pv_ja(b, pv, n), ""]
            if first["king"]:
                text.append(f"※詰将棋には先手玉がないため、先手玉を{first['king']}に補って探索しました。")
                if len(runs) > 1:
                    s2 = runs[1]
                    if s2["state"] == "mate" and s2["pv"] == pv:
                        text.append(f"玉を{s2['king']}に置き換えて再探索しても、同じ手順でした（玉の影響なし）。")
                    elif s2["state"] == "mate":
                        text.append(f"玉を{s2['king']}に置き換えて再探索すると、{len(s2['pv'])}手詰・手順が異なりました。"
                                    "玉の位置の影響か、別解の可能性があります。")
                    else:
                        text.append(f"玉を{s2['king']}に置き換えた再探索は詰みを確認できませんでした"
                                    "（玉の位置の影響が出ている可能性があります）。")
                text.append("")
            cmp_ = r.get("compare")
            if cmp_:
                if list(cmp_) == list(pv):
                    text.append("**棋譜の手順と完全に一致しました。**")
                else:
                    k = next((i for i, (x, y) in enumerate(zip(cmp_, pv)) if x != y), min(len(cmp_), len(pv)))
                    text.append(f"棋譜は{len(cmp_)}手、エンジンは{n}手。{k + 1}手目から手順が違います"
                                f"（同じ手数の別解・余詰め・エンジンの手順が最短でない、のいずれかです）。")
            self.result.setMarkdown("\n".join(text))
            self.status.setText(f"{n}手詰を発見（{sec:.1f}秒）")
        elif st == "nomate":
            self.result.setMarkdown(f"## 詰みはありません（不詰）\n\n{sec:.1f}秒で、この局面は詰まないと判定されました。")
            self.status.setText("不詰と判定")
        elif st == "timeout":
            self.result.setMarkdown(f"## 詰みを見つけられませんでした\n\n{sec:.0f}秒で中止または時間切れです。"
                                    "設定の「詰み探索の時間」を長くするか、0（無制限）にして試してください。")
            self.status.setText("詰み探索：時間切れ／中止")
        else:
            self.result.setMarkdown("## 詰みの答えを得られませんでした\n\n"
                                    "エンジンが詰みの情報を返しませんでした。通常の検討（検討開始）も試してください。")
            self.status.setText("詰みの情報が得られませんでした")

    # ---------------------------------------------------------- 全局解析・評価値グラフ
    def sync_eval_graph(self):
        """棋譜が変わったらグラフを入れ替える（保存済みの解析結果があればそれを出す）。カーソルを合わせる。"""
        key = core.game_key(self.start_sfen, self.moves) if self.moves else None
        if key != self.eval_key:
            if self.game_running:
                self.game_cancel.set()
                self._stop_game_engines()
            self.eval_key = key
            self.evals, self.swings = None, {}
            self.game_label.setText("")
            if key:
                cache = core.load_eval_cache(self.save_dir(), key, len(self.moves))
                if cache:
                    self.evals = cache["evals"]
                    self.swings = core.classify_swings(self.start_sfen, self.moves, self.evals)
                    self.game_label.setText(f"保存済みの解析結果を表示中（1手{cache.get('movetime_ms', 0) / 1000:g}秒）")
            self.graph.set_data(self.evals, self.swings)
            self.refresh_move_list()
        self.graph.set_cursor(self.cursor)

    def open_eval_file(self):
        """保存した評価グラフ(JSON)を選んで開く。棋譜ごと読み込み、グラフを表示する。"""
        if self.game_running:
            QMessageBox.information(self, "案内", "解析中です。終わるか中止してから開いてください。")
            return
        path, _ = QFileDialog.getOpenFileName(self, "評価グラフを選ぶ", core.eval_cache_dir(self.save_dir()),
                                              "評価グラフ (*.json);;すべて (*)")
        if not path:
            return
        try:
            data = core.read_eval_file(path)
            core.board_from(data["start_sfen"], data["moves"])
        except Exception as e:
            QMessageBox.warning(self, "開けません", str(e))
            return
        self.start_sfen, self.moves = data["start_sfen"], list(data["moves"])
        self.names = list(data.get("names") or ["先手", "後手"])
        self.cursor = 0
        self.refresh(analyze=False)                      # この中で、保存場所にあれば自動で読まれる
        key = core.game_key(self.start_sfen, self.moves)
        self.eval_key = key
        self.evals = data["evals"]
        self.swings = core.classify_swings(self.start_sfen, self.moves, self.evals)
        self.graph.set_data(self.evals, self.swings)
        self.refresh_move_list()
        try:                                              # 別の場所のファイルでも、次からは自動で読めるようにする
            core.save_eval_cache(self.save_dir(), key, self.start_sfen, self.moves, self.names,
                                 data.get("movetime_ms", 0), self.evals)
        except Exception:
            pass
        self.game_label.setText(f"{len(self.moves)}手の評価グラフを開きました（1手{data.get('movetime_ms', 0) / 1000:g}秒）")
        self.graph.set_cursor(0)
        revs = data.get("reviews") or []
        extra = ""
        if revs:
            r = revs[-1]
            extra = (f"## 保存済みの講評（{r.get('ai', '?')} / {r.get('model', '?')}・{r.get('generated_at', '')}）\n\n"
                     + r.get("text", ""))
        self.show_game_summary(extra)

    def review_game(self):
        """全局解析の結果から、AIに一局の講評を書いてもらう。"""
        if self.game_running:
            QMessageBox.information(self, "案内", "解析中です。終わってから押してください。")
            return
        if not self.evals or any(e is None for e in self.evals):
            QMessageBox.information(self, "案内", "先に「全局を解析」を実行するか、「評価グラフを開く…」で結果を開いてください。")
            return
        if core.llm_key_missing(self.cfg):
            QMessageBox.information(self, "案内", f"{core.llm_name(self.cfg)}のAPIキーが未設定です。「設定…」で入力してください。")
            return
        key = self.eval_key
        try:
            prompt = core.build_review_prompt(self.names, self.start_sfen, self.moves, self.evals, self.swings,
                                                comments=self.current_kif_comments())
        except Exception as e:
            QMessageBox.warning(self, "講評の材料を作れません", str(e))
            return
        cfg = dict(self.cfg)
        self.btn_review.setEnabled(False)
        self.status.setText(f"{core.llm_name(cfg)}に講評を頼んでいます…（10〜30秒ほど）")

        def work():
            try:
                self.sig.review.emit(key, core.ask_llm(prompt, cfg))
            except Exception as e:
                self.sig.review_err.emit(str(e))

        threading.Thread(target=work, daemon=True).start()

    def on_review(self, key, text):
        self.btn_review.setEnabled(True)
        if key != self.eval_key:
            return                                   # 頼んでいる間に別の棋譜へ移った
        self.result.setMarkdown(text)
        model = {"openai": self.cfg.get("openai_model"), "claude": self.cfg.get("anthropic_model"),
                 "qwen": self.cfg.get("qwen_model"), "groq": self.cfg.get("groq_model"),
                 "mistral": self.cfg.get("mistral_model"),
                 "openrouter": self.cfg.get("openrouter_model")}.get(self.cfg.get("llm_provider"), self.cfg.get("gemini_model"))
        try:
            core.append_review(self.save_dir(), key, {"ai": core.llm_name(self.cfg), "model": model,
                                                      "generated_at": datetime.now().isoformat(timespec="seconds"),
                                                      "text": text})
            self.status.setText("講評を表示し、評価グラフのファイルに保存しました")
        except Exception as e:
            self.status.setText("講評を表示しました（保存に失敗：%s）" % e)

    def on_review_err(self, msg):
        self.btn_review.setEnabled(True)
        self.status.setText(f"{core.llm_name(self.cfg)}のエラー：" + msg)
        QMessageBox.warning(self, f"{core.llm_name(self.cfg)}のエラー", msg)

    def graph_tip(self, ply):
        e = self.evals[ply] if self.evals and 0 <= ply < len(self.evals) else None
        lines = [f"{ply}手目まで　評価値 {core.fmt_value(e)}"]
        if ply >= 1 and ply <= len(self.moves):
            try:
                bd = core.board_from(self.start_sfen, self.moves[:ply - 1])
                lines.append("指し手：" + core.move_ja(bd, self.moves[ply - 1]))
                sw = self.swings.get(ply)
                if sw:
                    best = core.move_ja(bd, sw["best"]) if sw["best"] else "?"
                    lines.append(f"{sw['name']}（{sw['loss']}点悪化）　最善は {best}")
            except Exception:
                pass
        return "\n".join(lines)

    def toggle_game_analysis(self):
        if self.game_running:
            self.game_cancel.set()
            self._stop_game_engines()
            self.game_label.setText("中止しています…")
            return
        if not self.moves:
            QMessageBox.information(self, "案内", "指し手のある棋譜を読み込んでください（KIF読込／KIF貼付）。")
            return
        key = core.game_key(self.start_sfen, self.moves)
        ms = self.game_ms()
        cache = core.load_eval_cache(self.save_dir(), key, len(self.moves))
        if cache:
            r = QMessageBox.question(self, "保存済みの解析があります",
                                     f"この棋譜は解析済みです（1手{cache.get('movetime_ms', 0) / 1000:g}秒）。\n"
                                     "「はい」＝保存済みの結果を使う　／　「いいえ」＝解析し直す",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                                     | QMessageBox.StandardButton.Cancel)
            if r == QMessageBox.StandardButton.Cancel:
                return
            if r == QMessageBox.StandardButton.Yes:
                self.eval_key = None
                self.sync_eval_graph()
                self.show_game_summary()
                return
        if self.analyzing and self.engine:
            self.engine.stop()
        self.gen += 1
        self.analyzing = False
        self.game_running = True
        self.game_cancel.clear()
        self.eval_key = key
        n = len(self.moves)
        self.evals, self.swings = [None] * (n + 1), {}
        self.graph.set_data(self.evals, {})
        self.refresh_move_list()
        self.btn_game.setText("解析を中止")
        nw0 = max(1, int(self.cfg.get("game_workers", 3)))
        est = (n + 1) * ms / 1000 / nw0 + 5
        self.game_label.setText(f"解析中… 0/{n + 1}局面（エンジン{nw0}個で並列・約{est:.0f}秒）")
        start_sfen, moves = self.start_sfen, list(self.moves)

        nw = max(1, int(self.cfg.get("game_workers", 3)))
        self.game_engines = []

        def work():
            try:
                path = self.cfg.get("engine_path", "")
                if not path:
                    raise RuntimeError("エンジンが登録されていません。右上の「設定…」から登録してください。")
                b = shogi.Board(start_sfen)
                sfens = [b.sfen()]
                for u in moves:
                    b.push(shogi.Move.from_usi(u))
                    sfens.append(b.sfen())
                todo = queue.Queue()
                for i, s in enumerate(sfens):
                    todo.put((i, s))
                opts = dict(self.cfg.get("engine_options", {}))
                opts["Threads"] = 1                              # 1つのエンジンは1スレッド。エンジンを複数動かして並列にする
                opts["USI_Hash"] = min(int(opts.get("USI_Hash", 256)), 256)
                errors = []

                def worker():
                    try:
                        eng = core.UsiEngine(path, opts)
                        self.game_engines.append(eng)
                        eng.start()
                        while not self.game_cancel.is_set():
                            try:
                                i, s = todo.get_nowait()
                            except queue.Empty:
                                break
                            e = core.eval_position(eng, s, ms)
                            if self.game_cancel.is_set():
                                break                      # 途中で止めた局面の結果は捨てる（短い思考で歪むため）
                            self.sig.game_prog.emit(key, i, e)
                    except Exception as ex:
                        errors.append(str(ex))
                        self.game_cancel.set()             # 1つでも失敗したら全体を止める
                        self._stop_game_engines()

                ts = [threading.Thread(target=worker, daemon=True) for _ in range(min(nw, len(sfens)))]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join()
                for eng in list(self.game_engines):
                    eng.close()
                self.game_engines = []
                if errors:
                    self.sig.game_err.emit(errors[0])
                else:
                    self.sig.game_done.emit(key, self.game_cancel.is_set())
            except Exception as ex:
                self.sig.game_err.emit(str(ex))

        threading.Thread(target=work, daemon=True).start()

    def _stop_game_engines(self):
        for eng in list(getattr(self, "game_engines", [])):
            try:
                eng.stop()
            except Exception:
                pass

    def game_ms(self):
        return max(100, int(float(self.cfg.get("game_sec", 1)) * 1000))

    def on_game_prog(self, key, i, entry):
        if key != self.eval_key or not self.evals or i >= len(self.evals):
            return
        self.evals[i] = entry
        self.graph.set_data(self.evals, {})
        self.refresh_move_list()
        self.game_label.setText(f"解析中… {i + 1}/{len(self.evals)}局面")

    def on_game_done(self, key, cancelled):
        self.game_running = False
        self.btn_game.setText("全局を解析（評価値グラフ）")
        if key != self.eval_key or not self.evals:
            return
        if cancelled:
            self.game_label.setText("中止しました（途中までの結果は保存していません）")
            return
        self.swings = core.classify_swings(self.start_sfen, self.moves, self.evals)
        self.graph.set_data(self.evals, self.swings)
        self.refresh_move_list()
        try:
            core.save_eval_cache(self.save_dir(), key, self.start_sfen, self.moves, self.names,
                                 self.game_ms(), self.evals)
            self.game_label.setText("解析完了（結果を保存しました）")
        except Exception as ex:
            self.game_label.setText(f"解析完了（保存に失敗: {ex}）")
        self.show_game_summary()

    def on_game_err(self, msg):
        self.game_running = False
        self.btn_game.setText("全局を解析（評価値グラフ）")
        self.game_label.setText("エラー：" + msg)
        QMessageBox.warning(self, "全局解析のエラー", msg)

    def show_game_summary(self, extra=""):
        """悪手などを、悪化の大きい順に右の欄へ出す。"""
        if not self.evals:
            return
        n = len(self.moves)
        lines = [f"## 全局解析の結果（{n}手）", ""]
        if not self.swings:
            lines.append("評価値が大きく悪化した手は見つかりませんでした。")
        else:
            lines.append("評価値が大きく悪化した手（悪化の大きい順。グラフの印をクリックすると、その局面に移動します）：")
            lines.append("")
            for i, s in sorted(self.swings.items(), key=lambda kv: -kv[1]["loss"])[:12]:
                bd = core.board_from(self.start_sfen, self.moves[:i - 1])
                best = core.move_ja(bd, s["best"]) if s["best"] else "?"
                lines.append(f"- **{i}手目 {core.move_ja(bd, s['usi'])}**　{s['name']}（{s['loss']}点悪化、"
                             f"{core.fmt_value({'v': s['before'], 'mate': None})}→"
                             f"{core.fmt_value({'v': s['after'], 'mate': None})}）　最善は {best}")
        if extra:
            lines += ["", "---", "", extra]
        self.result.setMarkdown("\n".join(lines))

    def stop_analysis(self):
        if self.engine:
            self.engine.stop()

    def on_cands(self, gen, cands, final):
        if gen != self.gen:
            return
        self.cands = cands
        row = max(0, self.list.currentRow())
        self.list.blockSignals(True)
        self.list.clear()
        b = self.cur_board
        for i, c in enumerate(cands, 1):
            self.list.addItem(f"{i}.  {core.fmt_eval(c, b.turn)}  （深さ{c['depth']}）\n    {core.pv_ja(b, c['pv'], 10)}")
        if cands:
            self.list.setCurrentRow(min(row, len(cands) - 1))
        self.list.blockSignals(False)
        self.on_pick_cand(self.list.currentRow())
        if final:
            self.analyzing = False
            self.cands_final = True
            self.status.setText("検討完了" if cands else "候補手を取得できませんでした")
            if self.explain_pending and cands:
                self.explain_pending = False
                self.start_gemini()

    def on_pick_cand(self, row):
        if 0 <= row < len(self.cands):
            self.boardw.set_preview(self.cands[row]["pv"][0])

    def on_error(self, gen, msg):
        if gen != self.gen:
            return
        self.analyzing = False
        self.explain_pending = False
        self.auto.setChecked(False)
        self.status.setText("エラー：" + msg)
        QMessageBox.warning(self, "エンジンのエラー", msg + "\n\n設定を直したら「検討開始」を押してください。")

    # ---------------------------------------------------------- Gemini解説
    def _prompt(self):
        game = {"names": self.names}
        return core.build_position_prompt(game, self.cur_board, self.cursor, self.cands, self.cfg,
                                          comment=self.kif_comment_here() or None)

    def explain(self):
        if core.llm_key_missing(self.cfg):
            QMessageBox.information(self, "案内", f"{core.llm_name(self.cfg)}のAPIキーが未設定です。「設定…」で入力してください。")
            return
        if self.cands_final and self.cands:
            self.start_gemini()
        else:
            self.request_analysis(explain_after=True)

    def start_gemini(self):
        gen = self.gen
        prompt = self._prompt()
        cfg = dict(self.cfg)
        self.btn_explain.setEnabled(False)
        self.status.setText(f"{core.llm_name(cfg)}に問い合わせ中…")

        def work():
            try:
                self.sig.gemini.emit(gen, core.ask_llm(prompt, cfg))
            except Exception as e:
                self.sig.gemini_err.emit(gen, str(e))

        threading.Thread(target=work, daemon=True).start()

    def on_gemini(self, gen, text):
        self.btn_explain.setEnabled(True)
        if gen != self.gen:
            return
        self.result.setMarkdown(text)
        try:
            path = self.save_explanation(text)
            self.status.setText("解説を表示し、保存しました：" + os.path.basename(path))
        except Exception as e:
            self.status.setText("解説を表示しました（保存に失敗：%s）" % e)

    def on_gemini_err(self, gen, msg):
        self.btn_explain.setEnabled(True)
        self.status.setText(f"{core.llm_name(self.cfg)}のエラー：" + msg)
        if gen == self.gen:
            QMessageBox.warning(self, f"{core.llm_name(self.cfg)}のエラー", msg)

    def copy_prompt(self):
        if not self.cands:
            QMessageBox.information(self, "案内", "先に検討を実行して、候補手を出してください。")
            return
        QApplication.clipboard().setText(self._prompt())
        self.status.setText("LLMに渡す文章をコピーしました（ClaudeなどのチャットにPasteしてください）")

    # ---------------------------------------------------------- 解説の保存と読み込み
    def save_dir(self):
        d = self.cfg.get("save_dir") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "解説保存")
        os.makedirs(d, exist_ok=True)
        return d

    def current_ply(self):
        """この局面の手数。SFENに手数が入っていればそれも足す（スクショ由来の1は「不明」とみなす）。"""
        try:
            base = int(self.start_sfen.split()[3])
        except (IndexError, ValueError):
            base = 1
        return self.cursor + (base - 1 if base > 1 else 0)

    def save_explanation(self, text):
        """解説した局面のSFEN・候補手（読み筋）・解説文を「日付-手数.json」に保存する。"""
        board = self.cur_board
        now = datetime.now()
        ply = self.current_ply()
        sfen = board.sfen()
        stem = now.strftime("%Y%m%d") + "-" + (f"{ply}手" if ply > 0 else now.strftime("%H%M%S"))
        folder = self.save_dir()
        path, n = os.path.join(folder, stem + ".json"), 1
        data = None
        while os.path.exists(path):          # 同じ名前で別の局面なら、番号を付けて別ファイルにする
            try:
                with open(path, "r", encoding="utf-8") as f:
                    old = json.load(f)
            except Exception:
                old = None
            if old and old.get("sfen") == sfen:
                data = old
                break
            n += 1
            path = os.path.join(folder, f"{stem}_{n}.json")
        if data is None:
            data = {"version": 1, "explanations": []}
        data.update({
            "date": now.strftime("%Y-%m-%d"), "ply": ply, "sfen": sfen,
            "turn": "先手" if board.turn == shogi.BLACK else "後手", "names": list(self.names)})
        if self.cands or "candidates" not in data:     # 候補手が空のときは、すでに保存してある読み筋を消さない
            data["candidates"] = [{
                "rank": i, "eval_text": core.fmt_eval(c, board.turn), "depth": c.get("depth", 0),
                "kind": c["kind"], "cp": c["cp"], "mate_sign": c["mate_sign"], "mate_n": c["mate_n"],
                "pv": list(c["pv"]), "pv_ja": core.pv_ja(board, c["pv"], 10),
            } for i, c in enumerate(self.cands, 1)]
        model = {"openai": self.cfg.get("openai_model"), "claude": self.cfg.get("anthropic_model"),
             "qwen": self.cfg.get("qwen_model"),
             "groq": self.cfg.get("groq_model"),
             "mistral": self.cfg.get("mistral_model"),
             "openrouter": self.cfg.get("openrouter_model")}.get(
            self.cfg.get("llm_provider"), self.cfg.get("gemini_model"))
        data["explanations"].append({"ai": core.llm_name(self.cfg), "model": model,
                                     "generated_at": now.isoformat(timespec="seconds"), "text": text})
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path

    def open_saved(self):
        path, _ = QFileDialog.getOpenFileName(self, "保存した解説を開く", self.save_dir(), "解説ファイル (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            shogi.Board(data["sfen"])
            cands = [{"kind": c["kind"], "cp": c["cp"], "mate_sign": c["mate_sign"], "mate_n": c["mate_n"],
                      "depth": c.get("depth", 0), "pv": list(c["pv"]), "multipv": c.get("rank", 1)}
                     for c in data.get("candidates", [])]
        except Exception as e:
            QMessageBox.warning(self, "読み込めません", f"解説ファイルを読めませんでした。\n{e}")
            return
        self.start_sfen, self.moves, self.cursor = data["sfen"], [], 0
        self.names = data.get("names") or ["先手", "後手"]
        self.refresh(analyze=False)
        self.on_cands(self.gen, cands, False)
        self.cands_final = True
        parts = []
        for ex in data.get("explanations", []):
            parts.append(f"## {ex.get('ai', '')}（{ex.get('model', '')}）　{str(ex.get('generated_at', '')).replace('T', ' ')}\n\n{ex.get('text', '')}")
        self.result.setMarkdown("\n\n---\n\n".join(parts) if parts else "（解説は保存されていません）")
        self.info.setText(f"保存した解説：{data.get('date', '')}　{data.get('ply', '')}手目　{data.get('turn', '')}の手番")
        self.status.setText("読み込みました：" + os.path.basename(path))

    # ---------------------------------------------------------- 設定・終了
    def update_ai_labels(self):
        """選んでいる解説AIの名前を、ボタンと説明欄に出す。"""
        name = core.llm_name(self.cfg)
        self.btn_explain.setText(f"この局面を解説する（{name}）")
        self.result.setPlaceholderText(f"ここに{name}の解説が出ます")

    def open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec():
            dlg.apply_to(self.cfg)
            save_config(self.cfg)
            self.update_ai_labels()
            self.gen += 1
            self.close_engine()
            self.analyzing = False
            self.auto.setChecked(True)
            self.status.setText("設定を保存しました")

    def closeEvent(self, e):
        self.gen += 1
        try:
            save_config(self.cfg)
        except OSError:
            pass
        self.close_engine()
        e.accept()


def main():
    app = QApplication(sys.argv)
    try:   # アプリ専用のアイコンを、ウインドウとタスクバーに付ける（kento.ico が無くても動く）
        from app_icon import apply_app_icon
        apply_app_icon(app, "kento.ico", "Muramatsu.KentoShogi", folder=os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass
    f = app.font()
    f.setFamilies(FONT_FAMILIES)
    f.setPointSize(10)
    app.setFont(f)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        with open(os.path.join(HERE, "error.log"), "w", encoding="utf-8") as fh:
            fh.write(traceback.format_exc())
        raise
