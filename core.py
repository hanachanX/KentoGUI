# -*- coding: utf-8 -*-
"""将棋エンジン解析 → LLM(Gemini)解説 の中核部分（画面なし）。

流れ：KIF読込 → USIエンジンで解析 → 解析結果を文章(プロンプト)にまとめる → Geminiに渡す
"""
import base64
import json
import os
import queue
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

import shogi
import shogi.KIF as KIF

FILE_ZEN = "０１２３４５６７８９"
RANK_KAN = "〇一二三四五六七八九"
MOVE_NAME = {"P": "歩", "L": "香", "N": "桂", "S": "銀", "G": "金", "B": "角", "R": "飛", "K": "玉",
             "+P": "と", "+L": "成香", "+N": "成桂", "+S": "成銀", "+B": "馬", "+R": "龍"}
BOARD_NAME = {"P": "歩", "L": "香", "N": "桂", "S": "銀", "G": "金", "B": "角", "R": "飛", "K": "玉",
              "+P": "と", "+L": "杏", "+N": "圭", "+S": "全", "+B": "馬", "+R": "龍"}
HAND_ORDER = [(7, "飛"), (6, "角"), (5, "金"), (4, "銀"), (3, "桂"), (2, "香"), (1, "歩")]
MATE_BASE = 30000

DEFAULT_CONFIG = {
    "engine_path": "",
    "engine_options": {"Threads": 4, "USI_Hash": 512},
    "movetime_sec": 5,
    "mate_sec": 60,
    "game_sec": 1,
    "game_workers": 3,
    "multipv": 3,
    "pv_moves": 8,
    "grade_a_under": 100,
    "grade_b_under": 300,
    "screenshot_mode": "kiou",
    "llm_provider": "gemini",
    "anthropic_api_key": "",
    "anthropic_model": "claude-sonnet-5-5",
    "qwen_api_key": "",
    "qwen_model": "qwen3.8-max",
    "mistral_api_key": "",
    "mistral_model": "open-mistral-nemo",
    "groq_api_key": "",
    "groq_model": "openai/gpt-oss-120b",
    "qwen_base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "openai_api_key": "",
    "openai_model": "gpt-5.4-mini",
    "gemini_api_key": "",
    "gemini_model": "gemini-2.5-flash",
    "openrouter_api_key": "",
    "openrouter_model": "nvidia/nemotron-3-ultra-550b-a55b:free",
}


# ---------------------------------------------------------------- 設定
def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as f:
            cfg.update(json.load(f))
    if not cfg.get("gemini_api_key"):
        cfg["gemini_api_key"] = os.environ.get("GEMINI_API_KEY", "")
    if not cfg.get("openai_api_key"):
        cfg["openai_api_key"] = os.environ.get("OPENAI_API_KEY", "")
    if not cfg.get("anthropic_api_key"):
        cfg["anthropic_api_key"] = os.environ.get("ANTHROPIC_API_KEY", "")
    if not cfg.get("mistral_api_key"):
        cfg["mistral_api_key"] = os.environ.get("MISTRAL_API_KEY", "")
    if not cfg.get("groq_api_key"):
        cfg["groq_api_key"] = os.environ.get("GROQ_API_KEY", "")
    if not cfg.get("qwen_api_key"):
        cfg["qwen_api_key"] = os.environ.get("QWEN_API_KEY", "") or os.environ.get("DASHSCOPE_API_KEY", "")
    if not cfg.get("openrouter_api_key"):
        cfg["openrouter_api_key"] = os.environ.get("OPENROUTER_API_KEY", "")
    return cfg


# ---------------------------------------------------------------- 棋譜
def _read_text(path):
    data = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp932"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


def load_kif(path):
    return load_kif_text(_read_text(path))


_KIF_MOVE_LINE = re.compile(r"\A\s*([0-9]+)\s+\S")


def extract_kif_comments(text):
    """KIFの「*」で始まるコメント行を、手数ごとにまとめる。 {手数: 文章}
    0 は最初の局面（対局前）のコメント。「変化：」以降（枝分かれの手順）は読まない。"""
    out, ply, buf = {}, 0, []

    def flush():
        s = "\n".join(buf).strip("\n")
        if s.strip():
            out[ply] = (out[ply] + "\n\n" + s) if ply in out else s
        buf.clear()

    for line in (text or "").splitlines():
        if line.startswith("変化"):
            break
        if line.startswith("*"):
            buf.append(line[1:])
            continue
        m = _KIF_MOVE_LINE.match(line)
        if m and not line.lstrip().startswith("#"):
            flush()
            ply = int(m.group(1))
    flush()
    return out


def load_kif_text(text):
    """KIFの文章（ファイルの中身でも、クリップボードに貼った文章でも）を読む。
    指し手が無くても、盤面の図があれば「局面だけ」読み込む（詰将棋の初形など）。"""
    text = (text or "").replace("\u3000", "　").strip("\ufeff\r\n ")
    try:
        games = KIF.Parser.parse_str(text)
    except Exception as e:
        raise ValueError(f"棋譜として読めませんでした（{e}）")
    if not games:
        raise ValueError("棋譜から局面も指し手も読み取れませんでした")
    g = games[0]
    if not g.get("moves"):
        # 指し手なし＝図だけ。詰将棋のように先手玉が無く後手玉がある図は、先手番にそろえる
        parts = g["sfen"].split(" ")
        bd = shogi.Board(g["sfen"])
        if len(parts) >= 2 and bd.king_squares[shogi.BLACK] is None and bd.king_squares[shogi.WHITE] is not None:
            parts[1] = "b"
            g["sfen"] = " ".join(parts)
        if g["sfen"] == shogi.STARTING_SFEN and "平手" not in text and "▲" not in text and "手数" not in text:
            raise ValueError("棋譜から局面も指し手も読み取れませんでした")
    names = g.get("names") or ["", ""]
    sfen = g["sfen"]
    try:
        board_from(sfen, g["moves"])
    except Exception:
        # 詰将棋のように初形を図で持つ棋譜では、手番が逆に読まれることがある。逆にして読み直す
        parts = sfen.split(" ")
        if len(parts) >= 2 and parts[1] in ("b", "w"):
            parts[1] = "w" if parts[1] == "b" else "b"
            alt = " ".join(parts)
            board_from(alt, g["moves"])          # これも失敗なら元のエラーではなくこちらのエラーが出る
            sfen = alt
        else:
            raise
    return {"names": [names[0] or "先手", names[1] or "後手"], "sfen": sfen, "moves": g["moves"],
            "comments": extract_kif_comments(text)}


def board_from(sfen, moves):
    b = shogi.Board(sfen)
    for m in moves:
        b.push(shogi.Move.from_usi(m))
    return b


def _sq(name):
    return shogi.SQUARE_NAMES.index(name)


# ---------------------------------------------------------------- 表記
def move_ja(board, usi):
    """USI形式の指し手を、KIF風の日本語（例：▲７六歩(77)、△２二角打）にする。"""
    side = "▲" if board.turn == shogi.BLACK else "△"
    dest = FILE_ZEN[int(usi[2])] + RANK_KAN[ord(usi[3]) - 96]
    if usi[1] == "*":
        return f"{side}{dest}{MOVE_NAME[usi[0]]}打"
    piece = board.piece_at(_sq(usi[0] + usi[1]))
    name = MOVE_NAME[piece.symbol().upper()]
    promo = "成" if usi.endswith("+") else ""
    return f"{side}{dest}{name}{promo}({usi[0]}{ord(usi[1]) - 96})"


def pv_ja(board, pv, limit):
    b = shogi.Board(board.sfen())
    out = []
    for usi in pv[:limit]:
        try:
            mv = shogi.Move.from_usi(usi)
            if mv not in b.legal_moves:
                break
            out.append(move_ja(b, usi))
            b.push(mv)
        except Exception:
            break
    return " ".join(out)


def move_labels(game):
    b = shogi.Board(game["sfen"])
    labels = ["開始局面"]
    for i, usi in enumerate(game["moves"], 1):
        labels.append(f"{i}手目 {move_ja(b, usi)}")
        b.push(shogi.Move.from_usi(usi))
    return labels


def board_text(board):
    lines = ["   ９  ８  ７  ６  ５  ４  ３  ２  １"]
    for r in range(1, 10):
        cells = []
        for f in range(9, 0, -1):
            p = board.piece_at(_sq(f"{f}{chr(96 + r)}"))
            if p is None:
                cells.append(" ・")
            else:
                name = BOARD_NAME[p.symbol().upper()]
                cells.append(("v" if p.color == shogi.WHITE else " ") + name)
        lines.append(" " + " ".join(cells) + " " + RANK_KAN[r])
    lines.append("（vが付いた駒が後手、付かない駒が先手）")
    return "\n".join(lines)


def hand_text(board, color):
    parts = []
    for pt, name in HAND_ORDER:
        n = board.pieces_in_hand[color].get(pt, 0)
        if n:
            parts.append(name + (str(n) if n > 1 else ""))
    return " ".join(parts) if parts else "なし"


# ---------------------------------------------------------------- 評価値
def eval_stm(c):
    """手番側から見た評価値（数値）。詰みは大きな値にする。"""
    if c["kind"] == "mate":
        n = c["mate_n"] if c["mate_n"] is not None else 100
        return c["mate_sign"] * (MATE_BASE - n)
    return c["cp"]


def fmt_eval(c, turn):
    """先手から見た評価値の文字列。"""
    sign_b = 1 if turn == shogi.BLACK else -1
    if c["kind"] == "mate":
        who = "先手" if c["mate_sign"] * sign_b > 0 else "後手"
        n = f"{c['mate_n']}手" if c["mate_n"] is not None else "手数不明の"
        return f"{who}の{n}詰み"
    return f"{c['cp'] * sign_b:+d}"


def grade(loss, cfg):
    if loss < cfg["grade_a_under"]:
        return "A"
    if loss < cfg["grade_b_under"]:
        return "B"
    return "C"


# ---------------------------------------------------------------- USIエンジン
def parse_info(line):
    t = line.split()
    if len(t) < 2 or t[1] == "string" or "pv" not in t or "score" not in t:
        return None
    c = {"multipv": 1, "depth": 0, "kind": "cp", "cp": 0, "mate_sign": 1, "mate_n": None, "pv": []}
    i = 1
    try:
        while i < len(t):
            k = t[i]
            if k == "depth":
                c["depth"] = int(t[i + 1]); i += 2
            elif k == "multipv":
                c["multipv"] = int(t[i + 1]); i += 2
            elif k == "score":
                kind, val = t[i + 1], t[i + 2]
                if kind == "cp":
                    c["kind"] = "cp"; c["cp"] = int(val)
                elif kind == "mate":
                    c["kind"] = "mate"
                    if val in ("+", "-"):
                        c["mate_sign"] = 1 if val == "+" else -1; c["mate_n"] = None
                    else:
                        n = int(val); c["mate_sign"] = 1 if n > 0 else -1; c["mate_n"] = abs(n)
                i += 3
            elif k == "pv":
                c["pv"] = t[i + 1:]
                break
            else:
                i += 1
    except (ValueError, IndexError):
        return None
    return c if c["pv"] else None


class UsiEngine:
    def __init__(self, path, options=None):
        self.path = path
        self.options = options or {}
        self.proc = None
        self.q = queue.Queue()
        self._lock = threading.Lock()

    def _send(self, s):
        with self._lock:
            self.proc.stdin.write(s + "\n")
            self.proc.stdin.flush()

    def stop(self):
        """解析中のエンジンに「止めて」と伝える（すぐに結果が返る）。"""
        try:
            if self.proc and self.proc.poll() is None:
                self._send("stop")
        except Exception:
            pass

    def _reader(self):
        for line in self.proc.stdout:
            self.q.put(line.strip())
        self.q.put(None)

    def _wait_for(self, token, timeout):
        end = time.time() + timeout
        while time.time() < end:
            try:
                line = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                raise RuntimeError("エンジンが終了しました（エンジンのパスや評価関数の場所を確認してください）")
            if line.startswith(token):
                return
        raise TimeoutError(f"エンジンから {token} が返ってきません")

    def start(self):
        if not os.path.exists(self.path):
            raise FileNotFoundError(f"エンジンが見つかりません: {self.path}")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            [self.path], cwd=os.path.dirname(os.path.abspath(self.path)) or None,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags)
        threading.Thread(target=self._reader, daemon=True).start()
        self._send("usi")
        self._wait_for("usiok", 30)
        for k, v in self.options.items():
            self._send(f"setoption name {k} value {v}")
        self._send("isready")
        self._wait_for("readyok", 180)
        self._send("usinewgame")

    def analyze(self, sfen, movetime_ms, multipv, on_info=None):
        """局面を解析し、(候補手のリスト, bestmove) を返す。on_infoを渡すと、解析の途中経過も受け取れる。"""
        while not self.q.empty():
            self.q.get_nowait()
        self._send(f"setoption name MultiPV value {multipv}")
        self._send(f"position sfen {sfen}")
        self._send(f"go movetime {int(movetime_ms)}")
        best, bm, stopped, last_cb = {}, "", False, 0.0
        deadline = time.time() + movetime_ms / 1000 + 30
        while True:
            try:
                line = self.q.get(timeout=1.0)
            except queue.Empty:
                if time.time() > deadline:
                    if stopped:
                        raise TimeoutError("エンジンが応答しません")
                    self._send("stop"); stopped = True; deadline = time.time() + 10
                continue
            if line is None:
                raise RuntimeError("エンジンが途中で終了しました")
            if line.startswith("info"):
                c = parse_info(line)
                if c:
                    best[c["multipv"]] = c
                    if on_info and time.time() - last_cb > 0.3:
                        last_cb = time.time()
                        on_info([best[k] for k in sorted(best)])
            elif line.startswith("bestmove"):
                p = line.split()
                bm = p[1] if len(p) > 1 else ""
                break
        return [best[k] for k in sorted(best)], bm

    def mate(self, sfen, ms):
        """詰み専用の探索（USIの go mate）。戻り値 (状態, 手順のリスト, 詰みの手数(半手)または None)。
        状態: "mate"=詰みあり / "nomate"=不詰 / "timeout"=時間切れ・中止 / "unsupported"=このエンジンは未対応
        ※ 水匠は go mate を通常の探索のように処理し、info の「score mate N」と bestmove で答える。"""
        while not self.q.empty():
            self.q.get_nowait()
        self._send(f"position sfen {sfen}")
        self._send("go mate infinite" if ms <= 0 else f"go mate {int(ms)}")
        deadline = time.time() + (ms / 1000 if ms > 0 else 86400) + 60
        stopped = False
        best = None                       # 最後に出た「score mate N … pv …」
        while True:
            try:
                line = self.q.get(timeout=1.0)
            except queue.Empty:
                if time.time() > deadline:
                    if stopped:
                        raise TimeoutError("エンジンが応答しません")
                    self._send("stop"); stopped = True; deadline = time.time() + 15
                continue
            if line is None:
                raise RuntimeError("エンジンが途中で終了しました")
            if line.startswith("info"):
                t = line.split()
                if "score" in t and "pv" in t:
                    try:
                        k = t.index("score")
                        if t[k + 1] == "mate":
                            n = int(t[k + 2])
                            pv = t[t.index("pv") + 1:]
                            best = (n, pv)
                    except (ValueError, IndexError):
                        pass
            elif line.startswith("checkmate"):
                p = line.split()[1:]
                if not p or p[0] == "timeout":
                    return "timeout", [], None
                if p[0] == "nomate":
                    return "nomate", [], None
                if p[0] == "notimplemented":
                    return "unsupported", [], None
                return "mate", p, len(p)
            elif line.startswith("bestmove"):
                if best and best[0] > 0 and best[1]:
                    return "mate", best[1], best[0]
                if best and best[0] < 0:
                    return "nomate", [], None
                return ("timeout" if stopped else "unsupported"), [], None

    def close(self):
        try:
            if self.proc and self.proc.poll() is None:
                self._send("quit")
                self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def mate_king_candidates(sfen, n=2):
    """詰将棋は先手(攻方)の玉が無い。水匠は玉の無い局面で落ちるので、じゃまにならない場所に玉を補う。
    戻り値: [(補った後のSFEN, 玉の升の名前 or None), ...]（最大n個。玉を違う場所に置いて結果を見比べるため）"""
    b = shogi.Board(sfen)
    wk = b.king_squares[shogi.WHITE]
    if b.king_squares[shogi.BLACK] is not None or wk is None:
        return [(b.sfen(), None)]
    free = [sq for sq in range(81) if b.piece_at(sq) is None and not b.is_attacked_by(shogi.WHITE, sq)]

    def dist(a, c):
        return max(abs(a % 9 - c % 9), abs(a // 9 - c // 9))

    def crowd(sq):
        return sum(1 for d in (-10, -9, -8, -1, 1, 8, 9, 10) if 0 <= sq + d < 81 and abs((sq + d) % 9 - sq % 9) <= 1
                   and b.piece_at(sq + d) is not None)

    if not free:
        return [(b.sfen(), None)]
    free.sort(key=lambda sq: (-dist(sq, wk), crowd(sq), sq))
    picks = [free[0]]
    if n >= 2 and len(free) > 1:
        picks.append(max(free[1:], key=lambda sq: (min(dist(sq, wk), dist(sq, free[0])), -crowd(sq), -sq)))
    out = []
    for sq in picks:
        c = shogi.Board(b.sfen())
        c.set_piece_at(sq, shogi.Piece(shogi.KING, shogi.BLACK))
        out.append((c.sfen(), shogi.SQUARE_NAMES[sq]))
    return out


# ---------------------------------------------------------------- KIFの書き出し
def _kan_num(n):
    d = "〇一二三四五六七八九"
    if n < 10:
        return d[n]
    if n < 20:
        return "十" + (d[n - 10] if n > 10 else "")
    return str(n)


def _hand_kif(board, color):
    hand = board.pieces_in_hand[color]
    parts = []
    for pt, name in HAND_ORDER:
        n = hand.get(pt, 0)
        if n:
            parts.append(name + (_kan_num(n) if n > 1 else ""))
    return "　".join(parts) if parts else "なし"


def _bod_lines(board):
    out = ["後手の持駒：" + _hand_kif(board, shogi.WHITE), "  ９ ８ ７ ６ ５ ４ ３ ２ １", "+---------------------------+"]
    for r in range(1, 10):
        cells = []
        for f in range(9, 0, -1):
            p = board.piece_at(_sq(f"{f}{chr(96 + r)}"))
            if p is None:
                cells.append(" ・")
            else:
                cells.append(("v" if p.color == shogi.WHITE else " ") + BOARD_NAME[p.symbol().upper()])
        out.append("|" + "".join(cells) + "|" + RANK_KAN[r])
    out.append("+---------------------------+")
    out.append("先手の持駒：" + _hand_kif(board, shogi.BLACK))
    out.append("先手番" if board.turn == shogi.BLACK else "後手番")
    return out


def _kif_move(board, usi, prev_dest):
    """KIF形式の1手（例：２六歩(27)、同　歩(87)、２三歩打、８八角成(22)）。"""
    dest = usi[2:4]
    where = "同　" if prev_dest == dest else FILE_ZEN[int(usi[2])] + RANK_KAN[ord(usi[3]) - 96]
    if usi[1] == "*":
        return f"{where}{MOVE_NAME[usi[0]]}打", dest
    piece = board.piece_at(_sq(usi[0] + usi[1]))
    name = MOVE_NAME[piece.symbol().upper()]
    return f"{where}{name}{'成' if usi.endswith('+') else ''}({usi[0]}{ord(usi[1]) - 96})", dest


def export_kif(start_sfen, moves, names, comments=None, evals=None, swings=None, pv_n=8):
    """棋譜をKIFの文章にする。
    comments: {手数: 元の棋譜のコメント}／evals・swings: 全局解析の結果があれば、各手のコメントに
    評価値と読み筋(候補1)・疑問手の印を付ける。"""
    comments, swings = comments or {}, swings or {}
    start = shogi.Board(start_sfen)
    std = start.sfen().split(" ")[0] == shogi.STARTING_SFEN.split(" ")[0] and start.turn == shogi.BLACK
    lines = ["# KENTOで書き出した棋譜"]
    if std:
        lines.append("手合割：平手")
    else:
        lines += _bod_lines(start)
    lines += [f"先手：{names[0]}", f"後手：{names[1]}", "手数----指手---------消費時間--"]

    def note(k, board):
        """k手目まで進んだ局面に付けるコメント（行のリスト）。"""
        out = []
        if k in comments:
            old = comments[k].split("\n")
            if evals or swings:      # 前にKENTOで書き出したコメントは、新しい解析結果で置き換える
                old = [t for t in old if not re.match(r"【(KENTO|疑問手|悪手|大悪手)", t)]
                while old and not old[-1].strip():
                    old.pop()
            out += old
        extra = []
        sw = swings.get(k)
        if sw:
            try:
                prev = board_from(start_sfen, moves[:k - 1])
                mv = move_ja(prev, sw["usi"])
                best = move_ja(prev, sw["best"]) if sw.get("best") else ""
                s = (f"【{sw['name']}{sw['grade']}】{mv}で評価値が{sw['before']:+d}から{sw['after']:+d}に動きました"
                     + (f"（最善は{best}）" if best else ""))
                extra.append(s)
            except Exception:
                pass
        e = evals[k] if evals and k < len(evals) else None
        if e:
            s = f"【KENTO】評価値 {fmt_value(e)}"
            if e.get("pv"):
                ja = pv_ja(board, e["pv"], pv_n)
                if ja:
                    s += f"　読み筋：{ja}"
            extra.append(s)
        if extra:
            if out:
                out.append("")
            out += extra
        return out

    board = shogi.Board(start_sfen)
    for ln in note(0, board):
        lines.append("*" + ln)
    prev_dest = None
    for i, usi in enumerate(moves, 1):
        txt, prev_dest = _kif_move(board, usi, prev_dest)
        lines.append(f"{i:4d} {txt}")
        board.push(shogi.Move.from_usi(usi))
        for ln in note(i, board):
            lines.append("*" + ln)
    tail = [k for k in sorted(comments) if k > len(moves)]
    if tail:
        lines.append(f"{len(moves) + 1:4d} 投了")
        for k in tail:
            lines += ["*" + t for t in comments[k].split("\n")]
    return "\n".join(lines) + "\n"


def kif_bytes(text, utf8):
    """ファイルに書く中身。KIF(.kif)は昔からの文字コードShift-JIS、KIFU(.kifu)はUTF-8。"""
    if utf8:
        return text.encode("utf-8-sig")
    fix = {"〜": "～", "−": "－", "—": "―", "‖": "∥", "¢": "￠", "£": "￡", "¬": "￢"}
    for a, b in fix.items():
        text = text.replace(a, b)
    return text.encode("cp932", errors="replace")


# ---------------------------------------------------------------- 全局解析（評価値グラフ）
MATE_VALUE = 30000          # 詰みを数値にするときの大きさ（先手勝ちが＋、後手勝ちが－）
SWING_CLIP = 1500           # 悪手の判定では、評価値をこの範囲に丸める（大差の中での増減を無視するため）
SWING_GRADES = [(800, "??", "大悪手"), (400, "?", "悪手"), (200, "?!", "疑問手")]


def game_key(start_sfen, moves):
    """棋譜（開始局面＋指し手）を表す短い識別子。解析結果の保存・照合に使う。"""
    import hashlib
    try:
        s = shogi.Board(start_sfen).sfen()
    except Exception:
        s = start_sfen
    return hashlib.sha1((s + " " + " ".join(moves)).encode("utf-8")).hexdigest()[:16]


def eval_for_black(c, turn):
    """エンジンの候補(手番側から見た評価)を、先手から見た数値にする。戻り値 (数値, 詰みの手数(先手勝ち＋/後手勝ち－) または None)。"""
    sign_b = 1 if turn == shogi.BLACK else -1
    if c["kind"] == "mate":
        who = c["mate_sign"] * sign_b          # ＋なら先手の詰み
        n = c["mate_n"] if c["mate_n"] is not None else 99
        return who * (MATE_VALUE - min(n, 1000)), who * n
    return c["cp"] * sign_b, None


def eval_position(engine, sfen, movetime_ms):
    """1つの局面を解析し、グラフ用の記録（先手から見た評価値・最善手・読み筋の先頭）を返す。"""
    b = shogi.Board(sfen)
    if safe_call(b.is_checkmate):
        v = -MATE_VALUE if b.turn == shogi.BLACK else MATE_VALUE      # 手番側が詰まされた
        return {"v": v, "mate": 0, "depth": 0, "best": "", "pv": []}
    cands, _ = engine.analyze(sfen, movetime_ms, 1)
    if not cands:
        return {"v": 0, "mate": None, "depth": 0, "best": "", "pv": []}
    c = cands[0]
    v, mate = eval_for_black(c, b.turn)
    return {"v": int(v), "mate": mate, "depth": c.get("depth", 0), "best": c["pv"][0] if c["pv"] else "",
            "pv": list(c["pv"][:8])}


def safe_call(fn):
    try:
        return fn()
    except Exception:
        return False


def fmt_value(e):
    """記録1件を、先手から見た表示用の文字列にする。"""
    if e is None:
        return "未解析"
    if e.get("mate") is None and abs(e["v"]) >= MATE_VALUE - 1000:
        return "先手の詰み" if e["v"] > 0 else "後手の詰み"
    if e.get("mate") is not None:
        n = e["mate"]
        if n == 0:
            return "詰み"
        return ("先手" if n > 0 else "後手") + f"の{abs(n)}手詰み"
    return f"{e['v']:+d}"


def classify_swings(start_sfen, moves, evals):
    """各手の前後で評価値がどれだけ悪化したか（指した側から見て）を調べ、疑問手・悪手・大悪手を返す。
    戻り値: {手数(1から): {"grade","name","loss","before","after","usi","best","mover"}}"""
    out = {}
    b = shogi.Board(start_sfen)
    for i, usi in enumerate(moves, 1):
        e0, e1 = (evals[i - 1] if i - 1 < len(evals) else None), (evals[i] if i < len(evals) else None)
        mover = b.turn
        if e0 and e1:
            c0 = max(-SWING_CLIP, min(SWING_CLIP, e0["v"]))
            c1 = max(-SWING_CLIP, min(SWING_CLIP, e1["v"]))
            loss = (c0 - c1) if mover == shogi.BLACK else (c1 - c0)
            for th, g, name in SWING_GRADES:
                if loss >= th and usi != e0.get("best"):
                    out[i] = {"grade": g, "name": name, "loss": int(loss), "before": e0["v"], "after": e1["v"],
                              "usi": usi, "best": e0.get("best", ""), "mover": "先手" if mover == shogi.BLACK else "後手"}
                    break
        try:
            b.push(shogi.Move.from_usi(usi))
        except Exception:
            break
    return out


def eval_cache_dir(folder):
    d = os.path.join(folder, "評価グラフ")
    os.makedirs(d, exist_ok=True)
    return d


def _find_eval_file(folder, key):
    """key を末尾に持つ保存ファイルを探す（古い名前 key.json にも対応）。"""
    import glob
    d = os.path.join(folder, "評価グラフ")
    hits = glob.glob(os.path.join(glob.escape(d), f"*{key}.json"))
    return hits[0] if hits else None


def save_eval_cache(folder, key, start_sfen, moves, names, movetime_ms, evals):
    from datetime import datetime
    data = {"key": key, "start_sfen": start_sfen, "moves": list(moves), "names": list(names),
            "movetime_ms": int(movetime_ms), "evals": evals}
    old = _find_eval_file(folder, key)
    path = old or os.path.join(eval_cache_dir(folder), f"{datetime.now():%Y%m%d}-{len(moves)}手-{key}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return path


def read_eval_file(path):
    """評価グラフのJSONファイルを読む。戻り値は保存時のデータ。形式が違えば ValueError。"""
    try:
        data = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"ファイルを読めません（{e}）")
    if not isinstance(data, dict) or not all(k in data for k in ("start_sfen", "moves", "evals")):
        raise ValueError("評価グラフのファイルではありません")
    if len(data["evals"]) != len(data["moves"]) + 1:
        raise ValueError("手数と評価値の数が合いません（壊れたファイルかもしれません）")
    shogi.Board(data["start_sfen"])
    return data


def load_eval_cache(folder, key, n_moves):
    """保存済みの解析結果を返す（無い・合わない場合は None）。"""
    try:
        p = _find_eval_file(folder, key)
        if not p:
            return None
        data = json.load(open(p, encoding="utf-8"))
        ev = data.get("evals")
        if data.get("key") != key or not isinstance(ev, list) or len(ev) != n_moves + 1:
            return None
        return data
    except Exception:
        return None


# ---------------------------------------------------------------- プロンプト作成
COMMENT_RULES = """【棋譜コメントについて】
- 「コメント」は、対局の中継記者や解説者が人間として書いた文章です。将棋の内容（手の意味・変化・形勢判断・対局者の感想）に関する部分だけを材料にしてください。食事や天気、プロフィール、配信の案内、リンクなど、将棋の内容でない部分は無視してください。
- コメントに書かれた変化手順は局面図で確かめられません。使うときは「コメントによると」と出典を示し、エンジンの読み筋と食い違う場合はエンジンを優先して、食い違いを指摘してください。
- 「局後の感想」は対局者本人の発言です。"""


def clean_comment(text, limit=1500):
    """棋譜コメントから、リンクの行（URLと、その見出し）を除き、長すぎれば切る。"""
    lines = [t.rstrip() for t in (text or "").split("\n")]
    keep = []
    for i, t in enumerate(lines):
        s = t.strip()
        if re.match(r"https?://\S+$", s):
            continue
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if s.startswith("【") and s.endswith("】") and re.match(r"https?://", nxt):
            continue
        keep.append(t)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()
    if len(out) > limit:
        out = out[:limit] + "…（以下省略）"
    return out


RULES = """あなたは将棋の解説者です。以下は将棋エンジンによる解析結果です。
【守ってほしいこと】
- 駒の配置は下の局面図を正とします。図にない駒や、読み筋にない手の話はしないでください。
- 評価値は先手から見た値です（プラスなら先手有利）。目安：±100〜200はやや有利、±500以上はほぼ決まり、1000以上は勝勢。
- エンジンの数値と読み筋を根拠に、将棋に詳しくない人にも分かる言葉で説明してください。"""


def header(game, board, k):
    turn = "先手" if board.turn == shogi.BLACK else "後手"
    return (f"【局面】{k}手目まで進んだ局面（次は{turn}の手番）\n"
            f"先手：{game['names'][0]}　後手：{game['names'][1]}\n"
            f"{board_text(board)}\n"
            f"先手の持ち駒：{hand_text(board, shogi.BLACK)}\n"
            f"後手の持ち駒：{hand_text(board, shogi.WHITE)}\n"
            f"SFEN: {board.sfen()}")


def candidates_text(board, cands, pv_n):
    if not cands:
        return "（候補手を取得できませんでした）"
    lines = []
    for i, c in enumerate(cands, 1):
        lines.append(f"{i}. 評価値 {fmt_eval(c, board.turn)}（探索の深さ {c['depth']}）　読み筋：{pv_ja(board, c['pv'], pv_n)}")
    return "\n".join(lines)


def make_position_prompt(engine, game, k, cfg):
    board = board_from(game["sfen"], game["moves"][:k])
    cands, _ = engine.analyze(board.sfen(), cfg["movetime_sec"] * 1000, cfg["multipv"])
    return build_position_prompt(game, board, k, cands, cfg)


def build_position_prompt(game, board, k, cands, cfg, comment=None):
    """解析済みの候補手から、局面解説用の文章を作る。comment は、この局面に付いていた棋譜コメント。"""
    cm = clean_comment(comment) if comment else ""
    if cm:
        return (RULES + "\n" + COMMENT_RULES + "\n\n" + header(game, board, k) + "\n\n【エンジンの候補手】\n"
                + candidates_text(board, cands, cfg["pv_moves"])
                + "\n\n【この局面に付いていた棋譜コメント】\n" + cm
                + "\n\n【お願い】\nこの局面の形勢を説明し、手番側が指すべき方針と、候補手の違いを解説してください。"
                "コメントに将棋の内容の解説があれば、エンジンの読み筋と照らし合わせて取り入れ、一致する点・食い違う点も述べてください。")
    return (RULES + "\n\n" + header(game, board, k) + "\n\n【エンジンの候補手】\n"
            + candidates_text(board, cands, cfg["pv_moves"])
            + "\n\n【お願い】\nこの局面の形勢を説明し、手番側が指すべき方針と、候補手の違いを解説してください。")


def make_move_prompt(engine, game, k, cfg):
    """k手目の指し手を評価する（k>=1）。"""
    usi = game["moves"][k - 1]
    before = board_from(game["sfen"], game["moves"][:k - 1])
    ms, mpv = cfg["movetime_sec"] * 1000, cfg["multipv"]
    cands, _ = engine.analyze(before.sfen(), ms, mpv)
    if not cands:
        raise RuntimeError("エンジンが候補手を返しませんでした")
    best_val = eval_stm(cands[0])
    played = next((c for c in cands if c["pv"][0] == usi), None)
    if played is not None:
        played_val = eval_stm(played)
    else:
        after = shogi.Board(before.sfen())
        after.push(shogi.Move.from_usi(usi))
        if after.is_checkmate():
            played_val = MATE_BASE
        else:
            ac, _ = engine.analyze(after.sfen(), ms, 1)
            played_val = -eval_stm(ac[0]) if ac else best_val
    loss = max(0, best_val - played_val)
    g = grade(loss, cfg)
    sign_b = 1 if before.turn == shogi.BLACK else -1
    played_black = played_val * sign_b
    played_str = "詰みに関わる大きな値" if abs(played_val) >= MATE_BASE - 200 else f"{played_black:+d}"
    note = "最善手と同じ手です" if cands[0]["pv"][0] == usi else f"最善手との差は約{loss}点です"
    return (RULES + "\n\n" + header(game, before, k - 1)
            + "\n\n【エンジンの候補手（この局面での最善候補）】\n" + candidates_text(before, cands, cfg["pv_moves"])
            + f"\n\n【実戦の手】{move_ja(before, usi)}\n実戦の手の評価値：{played_str}（先手から見た値）。{note}。\n"
            + f"判定：{g}（A=好手〜ほぼ最善／B=疑問手／C=悪手。差が{cfg['grade_a_under']}点未満ならA、{cfg['grade_b_under']}点未満ならB、それ以上はC）"
            + "\n\n【お願い】\n上の判定を変えずに、実戦の手が良かった(または悪かった)理由を、最善候補との違いを比べて解説してください。")


# ---------------------------------------------------------------- 全局のAI講評
REVIEW_RULES = """あなたは将棋の講評者です。以下は、将棋エンジンで一局を全局面解析した結果です。
【守ってほしいこと】
- 評価値は先手から見た値です（プラスなら先手有利）。目安：±100〜200はやや有利、±500以上はほぼ決まり、1000以上は勝勢。
- 「悪手」「疑問手」は、指した側から見て評価値がどれだけ悪化したかで機械的に決めた目安です。判定は変えないでください。
- 駒の配置は、各局面に添えた図とSFENを正とします。図にない駒や、読み筋にない手の話はしないでください。
- 読み筋はエンジンの出力です。その範囲を根拠に、将棋に詳しくない人にも分かる言葉で書いてください。
- 実戦の手と最善手の違いは、「何が起きるか（読み筋の結果）」で説明してください。"""


def build_review_prompt(names, start_sfen, moves, evals, swings, max_items=8, comments=None):
    """全局解析の結果（評価値の流れと、悪化の大きかった手）から、一局の講評を頼む文章を作る。"""
    n = len(moves)
    final = fmt_value(evals[-1])
    flow = []
    step = max(1, n // 12)
    for i in range(0, n + 1, step):
        flow.append(f"{i}手目{fmt_value(evals[i])}")
    if (n % step) != 0:
        flow.append(f"{n}手目{fmt_value(evals[n])}")
    top = sorted(swings.items(), key=lambda kv: -kv[1]["loss"])[:max_items]
    top.sort(key=lambda kv: kv[0])                    # 説明は手数順
    rank = {i: r for r, (i, _) in enumerate(sorted(top, key=lambda kv: -kv[1]["loss"]), 1)}
    comments = comments or {}
    parts = [REVIEW_RULES + (("\n" + COMMENT_RULES) if comments else ""), "",
             f"【対局】先手：{names[0]}　後手：{names[1]}　全{n}手",
             f"【評価値の流れ（先手から見た値）】" + " / ".join(flow),
             f"【最後の局面の評価値】{final}", ""]
    parts.append(f"【勝負所（評価値が大きく悪化した手。悪化の大きい順に番号を付けました。全{len(swings)}件のうち上位{len(top)}件）】")
    for i, s in top:
        before = board_from(start_sfen, moves[:i - 1])
        after = shogi.Board(before.sfen())
        after.push(shogi.Move.from_usi(s["usi"]))
        e0, e1 = evals[i - 1], evals[i]
        best_line = pv_ja(before, e0.get("pv", []), 8) if e0.get("pv") else "（なし）"
        real_line = pv_ja(after, e1.get("pv", []), 8) if e1.get("pv") else "（なし）"
        parts += [
            "", f"■ 悪化{rank[i]}番目：{i}手目 {s['mover']}の {move_ja(before, s['usi'])}　【{s['name']}】（{s['loss']}点悪化）",
            f"  評価値：{fmt_value(e0)} → {fmt_value(e1)}（先手から見た値）",
            f"  最善手：{move_ja(before, s['best']) if s['best'] else '不明'}　最善の読み筋：{best_line}",
            f"  実戦の手の後の読み筋：{real_line}",
            f"  指す直前の局面：{i - 1}手目まで　SFEN: {before.sfen()}",
            board_text(before),
            f"  先手の持ち駒：{hand_text(before, shogi.BLACK)}　後手の持ち駒：{hand_text(before, shogi.WHITE)}"]
        cm = clean_comment(comments.get(i), 900) if comments.get(i) else ""
        if cm:
            parts += [f"  【この手（{i}手目）に付いていた棋譜コメント】", cm]
    tail = "\n\n".join(comments[k] for k in sorted(comments) if k >= n and k > 0)
    tail = clean_comment(tail, 1200) if tail else ""
    if tail:
        parts += ["", "【終局付近の棋譜コメント】", tail]
    parts += ["", "【お願い】",
              "次の順で、日本語の講評を書いてください（全体で1500〜2500字）。",
              "1. 一局の流れ（序盤・中盤・終盤で、形勢がどう動いたか）",
              "2. 勝敗の分かれ目（上の勝負所から、特に大きかった3つ。なぜ評価値が動いたかを、最善手との違いで説明）",
              "3. 負けた側（形勢が下がった側）の課題と、勝った側の良かった点",
              "4. 次の対局に活かせる具体的なポイント3つ",
              "勝負所の手数は「◯手目」と書いてください。"]
    if comments:
        parts.append("棋譜コメントに将棋の内容の解説（手の意味や感想）がある勝負所は、それも踏まえて書いてください。")
    return "\n".join(parts)


def append_review(folder, key, review):
    """評価グラフの保存ファイルに、AIの講評を追記する。"""
    p = _find_eval_file(folder, key)
    if not p:
        raise FileNotFoundError("評価グラフの保存ファイルが見つかりません")
    data = json.load(open(p, encoding="utf-8"))
    data.setdefault("reviews", []).append(review)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return p


# ---------------------------------------------------------------- Gemini
def _gemini_call(parts, api_key, model, gen_cfg, timeout=120):
    if not api_key:
        raise RuntimeError("config.json の gemini_api_key が空です（設定画面で入力してください）")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = json.dumps({"contents": [{"parts": parts}], "generationConfig": gen_cfg}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "x-goog-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Gemini APIエラー {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
    try:
        ps = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in ps)
    except (KeyError, IndexError):
        raise RuntimeError(f"Geminiから回答を得られませんでした: {str(data)[:400]}")


def ask_gemini(prompt, api_key, model, timeout=120):
    return _gemini_call([{"text": prompt}], api_key, model, {"temperature": 0.3}, timeout)


OPENAI_MODELS = [   # 設定画面の選択肢（表示名は右のコメント）。欄に直接書いて、ここに無い名前を使うこともできる
    "gpt-5.4-mini",     # GPT-5.4 Mini（標準・安価）
    "gpt-6-astra",      # GPT-6 Astra
    "gpt-6.1-sol",      # GPT-6.1 Sol
    "gpt-6-luna",       # GPT-6 Luna
    "gpt-5.6-sol",      # GPT-5.6 Sol
    "gpt-5.6-terra",    # GPT-5.6 Terra
    "gpt-5.6-luna",     # GPT-5.6 Luna
    "gpt-5.5",          # GPT-5.5
    "gpt-5.5-pro",      # GPT-5.5 Pro
    "gpt-5.4",          # GPT-5.4
    "gpt-5.4-pro",      # GPT-5.4 Pro
    "gpt-5.2",          # GPT-5.2
    "gpt-5.2-pro",      # GPT-5.2 Pro
    "gpt-5",            # GPT-5（旧）
    "gpt-5-mini",       # GPT-5 Mini（旧）
    "gpt-5-nano",       # GPT-5 nano（旧）
    "gpt-4.1",          # GPT-4.1
    "gpt-4.1-mini",     # GPT-4.1 Mini
    "gpt-4o",           # GPT-4o（旧）
    "gpt-4o-mini",      # GPT-4o Mini（旧）
]


def ask_openai(prompt, api_key, model, timeout=300):
    """ChatGPT(OpenAI)のAPIに文章で問い合わせる。Pro系は専用の窓口(Responses API)を使う。"""
    if not api_key:
        raise RuntimeError("OpenAIのAPIキーが空です（設定画面で入力してください）")
    use_responses = "pro" in model.lower()
    if use_responses:
        url, payload = "https://api.openai.com/v1/responses", {"model": model, "input": prompt}
    else:
        url = "https://api.openai.com/v1/chat/completions"
        payload = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OpenAI APIエラー {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
    try:
        if use_responses:
            if data.get("output_text"):
                return data["output_text"]
            texts = [p.get("text", "") for item in data["output"] if item.get("type") == "message"
                     for p in item.get("content", []) if p.get("type") == "output_text"]
            if texts:
                return "".join(texts)
            raise KeyError("output")
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"OpenAIから回答を得られませんでした: {str(data)[:400]}")


CLAUDE_MODELS = ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"]   # 設定画面の選択肢（直接入力も可）


def ask_claude(prompt, api_key, model, timeout=180):
    """Claude(Anthropic)のAPIに文章で問い合わせる。混雑時は待ってやり直す。"""
    import time
    if not api_key:
        raise RuntimeError("AnthropicのAPIキーが空です（設定画面で入力してください）")
    body = json.dumps({"model": model, "max_tokens": 16000,
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    last = None
    for attempt in range(4):
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
            "content-type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            if not text.strip():
                raise RuntimeError(f"Claudeの回答が空でした: stop_reason={data.get('stop_reason')} / {str(data)[:500]}")
            return text
        except urllib.error.HTTPError as e:
            last = RuntimeError(f"Claude APIエラー {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
            if e.code not in (429, 500, 502, 503, 504, 529) or attempt == 3:
                break
        except RuntimeError:
            raise
        except Exception as e:
            last = e
            if attempt == 3:
                break
        time.sleep([5, 15, 30][attempt])
    raise last


QWEN_MODELS = ["qwen3.8-max", "qwen3.8-plus", "qwen3.8-flash"]   # 設定画面の選択肢（直接入力も可）


def ask_qwen(prompt, api_key, model, base_url, timeout=120):
    """Qwen(アリババ)のAPIに問い合わせる。OpenAI互換の窓口(base_url + /chat/completions)を使う。"""
    import time
    if not api_key:
        raise RuntimeError("QwenのAPIキーが空です（設定画面で入力してください）")
    url = (base_url or "").rstrip("/") + "/chat/completions"
    body = json.dumps({"model": model, "max_tokens": 8000, "enable_thinking": False,
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    last = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=body, headers={
            "content-type": "application/json", "Authorization": "Bearer " + api_key,
            "User-Agent": "Mozilla/5.0 KentoShogi"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            try:
                text = data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                text = ""
            if not text.strip():
                raise RuntimeError(f"Qwenの回答が空でした: {str(data)[:500]}")
            return text
        except urllib.error.HTTPError as e:
            last = RuntimeError(f"Qwen APIエラー {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
            if e.code not in (429, 500, 502, 503, 504) or attempt == 3:
                break
        except RuntimeError:
            raise
        except Exception as e:
            last = e
            if attempt == 3:
                break
        time.sleep([5, 15, 30][attempt])
    raise last


GROQ_MODELS = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]   # 設定画面の選択肢（直接入力も可）


def ask_groq(prompt, api_key, model, timeout=120):
    """Groq(高速なオープンモデル)のAPIに問い合わせる。OpenAI互換。回数制限(429)のときは待ってやり直す。"""
    import time
    if not api_key:
        raise RuntimeError("GroqのAPIキーが空です（設定画面で入力してください）")
    body = json.dumps({"model": model, "max_tokens": 8000,
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    last = None
    for attempt in range(4):
        req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions", data=body, headers={
            "content-type": "application/json", "Authorization": "Bearer " + api_key,
            "User-Agent": "Mozilla/5.0 KentoShogi"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            try:
                text = data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                text = ""
            if not text.strip():
                raise RuntimeError(f"Groqの回答が空でした: {str(data)[:500]}")
            return text
        except urllib.error.HTTPError as e:
            last = RuntimeError(f"Groq APIエラー {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
            if e.code not in (429, 500, 502, 503, 504) or attempt == 3:
                break
        except RuntimeError:
            raise
        except Exception as e:
            last = e
            if attempt == 3:
                break
        time.sleep([10, 30, 60][attempt])
    raise last


MISTRAL_MODELS = ["open-mistral-nemo", "ministral-8b-latest", "ministral-14b-latest"]   # 無料枠で通ったのは先頭の2つ（直接入力も可）


def ask_mistral(prompt, api_key, model, timeout=120):
    """Mistralのapiに問い合わせる。OpenAI互換。回数制限(429)のときは待ってやり直す。"""
    import time
    if not api_key:
        raise RuntimeError("MistralのAPIキーが空です（設定画面で入力してください）")
    body = json.dumps({"model": model, "max_tokens": 8000,
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    last = None
    for attempt in range(4):
        req = urllib.request.Request("https://api.mistral.ai/v1/chat/completions", data=body, headers={
            "content-type": "application/json", "Authorization": "Bearer " + api_key,
            "User-Agent": "Mozilla/5.0 KentoShogi"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            try:
                text = data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                text = ""
            if not text.strip():
                raise RuntimeError(f"Mistralの回答が空でした: {str(data)[:500]}")
            return text
        except urllib.error.HTTPError as e:
            last = RuntimeError(f"Mistral APIエラー {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
            if e.code not in (429, 500, 502, 503, 504) or attempt == 3:
                break
        except RuntimeError:
            raise
        except Exception as e:
            last = e
            if attempt == 3:
                break
        time.sleep([10, 30, 60][attempt])
    raise last


OPENROUTER_MODELS = ["nvidia/nemotron-3-ultra-550b-a55b:free", "nvidia/nemotron-3-super-120b-a12b:free"]   # 無料(:free)モデル。先頭が失敗したら次を自動で試す（直接入力も可）


def ask_openrouter(prompt, api_key, model, timeout=120):
    """OpenRouter(無料モデルあり)に問い合わせる。OpenAI互換。混雑(429)などで失敗したら、別の無料モデルを順に試す。"""
    import time
    if not api_key:
        raise RuntimeError("OpenRouterのAPIキーが空です（設定画面で入力してください）")
    cands = [model] + [m for m in OPENROUTER_MODELS if m != model]
    last = None
    for i, m in enumerate(cands):
        body = json.dumps({"model": m, "max_tokens": 8000,
                           "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
        for attempt in range(2):
            req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body, headers={
                "content-type": "application/json", "Authorization": "Bearer " + api_key,
                "User-Agent": "Mozilla/5.0 KentoShogi"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
                try:
                    text = data["choices"][0]["message"]["content"] or ""
                except (KeyError, IndexError, TypeError):
                    text = ""
                if text.strip():
                    return text if i == 0 else f"（※ {cands[0]} が使えなかったため {m} で回答しました）\n\n{text}"
                last = RuntimeError(f"OpenRouter({m})の回答が空でした: {str(data)[:300]}")
                break                                    # 次のモデルへ
            except urllib.error.HTTPError as e:
                last = RuntimeError(f"OpenRouter APIエラー {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
                if e.code in (401, 402, 403):            # キーや設定の問題は、別モデルでも直らない
                    raise last
                if e.code not in (429, 500, 502, 503, 504) or attempt == 1:
                    break
            except Exception as e:
                last = e
                if attempt == 1:
                    break
            time.sleep(10)
    raise last


def llm_name(cfg):
    return {"openai": "ChatGPT", "claude": "Claude", "qwen": "Qwen", "groq": "Groq", "mistral": "Mistral", "openrouter": "OpenRouter"}.get(cfg.get("llm_provider"), "Gemini")


def llm_key_missing(cfg):
    field = {"openai": "openai_api_key", "claude": "anthropic_api_key", "qwen": "qwen_api_key", "groq": "groq_api_key", "mistral": "mistral_api_key", "openrouter": "openrouter_api_key"}.get(cfg.get("llm_provider"), "gemini_api_key")
    key = cfg.get(field)
    return (not key) or "ここに" in key


def ask_llm(prompt, cfg):
    """設定で選ばれたAI(Gemini / ChatGPT / Claude / Qwen / Groq / Mistral)に解説を頼む。"""
    p = cfg.get("llm_provider")
    if p == "openai":
        return ask_openai(prompt, cfg.get("openai_api_key", ""), cfg.get("openai_model", "gpt-5.4-mini"))
    if p == "claude":
        return ask_claude(prompt, cfg.get("anthropic_api_key", ""), cfg.get("anthropic_model", "claude-sonnet-5-5"))
    if p == "openrouter":
        return ask_openrouter(prompt, cfg.get("openrouter_api_key", ""), cfg.get("openrouter_model") or OPENROUTER_MODELS[0])
    if p == "mistral":
        return ask_mistral(prompt, cfg.get("mistral_api_key", ""), cfg.get("mistral_model", "open-mistral-nemo"))
    if p == "groq":
        return ask_groq(prompt, cfg.get("groq_api_key", ""), cfg.get("groq_model", "openai/gpt-oss-120b"))
    if p == "qwen":
        return ask_qwen(prompt, cfg.get("qwen_api_key", ""), cfg.get("qwen_model", "qwen3.8-max"),
                        cfg.get("qwen_base_url") or DEFAULT_CONFIG["qwen_base_url"])
    return ask_gemini(prompt, cfg.get("gemini_api_key", ""), cfg.get("gemini_model", "gemini-2.5-flash"))


def ask_gemini_image(prompt, image_bytes, api_key, model, mime="image/png", timeout=180):
    parts = [{"text": prompt}, {"inline_data": {"mime_type": mime, "data": base64.b64encode(image_bytes).decode("ascii")}}]
    return _gemini_call(parts, api_key, model, {"temperature": 0, "responseMimeType": "application/json"}, timeout)


# ---------------------------------------------------------------- 局面データ（盤・持ち駒・手番）とSFEN
HAND_LETTER = {7: "R", 6: "B", 5: "G", 4: "S", 3: "N", 2: "L", 1: "P"}
MAX_COUNT = {"P": 18, "L": 4, "N": 4, "S": 4, "G": 4, "B": 2, "R": 2, "K": 2}
BASE_PT = {"P": 1, "L": 2, "N": 3, "S": 4, "G": 5, "B": 6, "R": 7, "K": 8}
BASE_JA = {"P": "歩", "L": "香", "N": "桂", "S": "銀", "G": "金", "B": "角", "R": "飛", "K": "玉"}
CHAR_TO_SYM = {"歩": "P", "香": "L", "桂": "N", "銀": "S", "金": "G", "角": "B", "飛": "R", "玉": "K", "王": "K",
               "と": "+P", "杏": "+L", "圭": "+N", "全": "+S", "馬": "+B", "龍": "+R", "竜": "+R"}


def board_to_model(board):
    """盤面を (升ごとの駒記号のリスト, 持ち駒, 手番) に分解する。駒記号は先手が大文字、後手が小文字。"""
    cells = [None] * 81
    for sq in range(81):
        p = board.piece_at(sq)
        if p is not None:
            cells[sq] = p.symbol()
    hands = {0: {}, 1: {}}
    for color in (0, 1):
        for pt, n in board.pieces_in_hand[color].items():
            if n:
                hands[color][pt] = n
    return cells, hands, board.turn


def model_to_sfen(cells, hands, turn):
    rows = []
    for r in range(9):
        s, empty = "", 0
        for c in range(9):
            sym = cells[r * 9 + c]
            if sym is None:
                empty += 1
            else:
                if empty:
                    s += str(empty)
                    empty = 0
                s += sym
        if empty:
            s += str(empty)
        rows.append(s)
    hs = ""
    for color in (0, 1):
        for pt in (7, 6, 5, 4, 3, 2, 1):
            n = hands[color].get(pt, 0)
            if n:
                letter = HAND_LETTER[pt].lower() if color == 1 else HAND_LETTER[pt]
                hs += (str(n) if n > 1 else "") + letter
    return "/".join(rows) + (" b " if turn == 0 else " w ") + (hs or "-") + " 1"


def validate_model(cells, hands):
    """駒の枚数・二歩・行き場のない駒などを検算し、(重大度, 説明) のリストを返す。"""
    out = []
    count = {k: 0 for k in MAX_COUNT}
    for sym in cells:
        if sym:
            count[sym.lstrip("+").upper()] += 1
    for color in (0, 1):
        for pt, n in hands[color].items():
            for base, v in BASE_PT.items():
                if v == pt:
                    count[base] += n
    for base, mx in MAX_COUNT.items():
        if count[base] > mx:
            out.append(("error", f"{BASE_JA[base]}が{count[base]}枚あります（最大{mx}枚）。読み違いの可能性があります"))
        elif count[base] < mx:
            out.append(("warn", f"{BASE_JA[base]}が{count[base]}枚しかありません（通常は{mx}枚）。詰将棋の問題なら問題ありません"))
    kings = {0: 0, 1: 0}
    for sq, sym in enumerate(cells):
        if sym and sym.upper() == "K":
            kings[0 if sym == "K" else 1] += 1
    for color, name in ((0, "先手"), (1, "後手")):
        if kings[color] == 0:
            out.append(("warn", f"{name}の玉が見当たりません"))
        elif kings[color] > 1:
            out.append(("error", f"{name}の玉が{kings[color]}枚あります"))
    for color, mark in ((0, "P"), (1, "p")):
        for col in range(9):
            n = sum(1 for r in range(9) if cells[r * 9 + col] == mark)
            if n > 1:
                out.append(("error", f"{'先手' if color == 0 else '後手'}の歩が{9 - col}筋に{n}枚あり、二歩です"))
    for sq, sym in enumerate(cells):
        if not sym or sym.startswith("+"):
            continue
        rank = sq // 9 + 1
        black = sym.isupper()
        up = sym.upper()
        dead_rank = (rank == 1 if black else rank == 9) and up in ("P", "L")
        dead_rank = dead_rank or (up == "N" and (rank <= 2 if black else rank >= 8))
        if dead_rank:
            out.append(("error", f"{9 - sq % 9}筋{RANK_KAN[rank]}段の{BASE_JA[up]}は、動ける先がありません（読み違いの可能性）"))
    return out


# ---------------------------------------------------------------- スクショ → SFEN
SHOT_PROMPT = """これは将棋の対局画面のスクリーンショットです。盤面を「画面に見えるままの向き」で読み取り、JSONだけを返してください（説明は不要です）。

{
  "cells": 9行×9列の配列。画面に見える盤の上の行から下の行へ、各行は左の列から右の列へ順に並べる。
           各マスは、空なら ""、駒があれば「駒の漢字1文字＋向き1文字」の文字列。
           駒の漢字は 歩 香 桂 銀 金 角 飛 玉 王 と 杏 圭 全 馬 龍 竜 のいずれか（成り駒は成った後の字）。
           向きは、駒の尖った先端が画面の上を向いていれば "u"、下を向いていれば "d"。文字が逆さまに描かれている駒は "d"。
           例："歩u"、"銀d"、"馬u"
  "flipped": 筋の数字が画面の左から右へ 1,2,…,9 の順、または段の漢字が下から 一,二,…,九 の順なら true。
             通常（左から 9,8,…,1、上から 一,二,…,九）なら false。判断できなければ false。
  "hands": {"bottom": {...}, "top": {...}}  盤の下側・上側に表示されている持ち駒。キーは駒の漢字、値は枚数。無ければ {}。
           （持ち駒は盤の外、画面の隅などに表示されていることがあります）
  "last_move": 直前に指された手の「行き先のマス」が色や枠で強調されていれば {"row": 行(1〜9、画面の上から数える), "col": 列(1〜9、画面の左から数える)}。分からなければ null。
}

注意：
- 1マスずつ丁寧に確認し、駒の見落としや見間違いをしないでください。
- 成り駒（と・杏・圭・全・馬・龍）と、成っていない駒（歩・香・桂・銀・角・飛）を区別してください。
- 銀と金、桂と圭、角と馬、飛と龍は特に間違えやすいので注意してください。
"""


def model_from_screenshot_json(data):
    cells_in = data.get("cells")
    if not (isinstance(cells_in, list) and len(cells_in) == 9
            and all(isinstance(r, list) and len(r) == 9 for r in cells_in)):
        raise ValueError("盤面を9×9のマスとして読み取れませんでした")
    flipped = bool(data.get("flipped", False))
    bottom = 1 if flipped else 0   # 画面の下側にいるのは、通常は先手、反転表示なら後手
    cells, problems = [None] * 81, []
    for row in range(9):
        for col in range(9):
            raw = str(cells_in[row][col] or "").strip()
            if not raw:
                continue
            sym = CHAR_TO_SYM.get(raw[0])
            if sym is None:
                problems.append(f"{row + 1}行目・{col + 1}列目の「{raw}」を駒として読めませんでした")
                continue
            up = not raw[1:].strip().lower().startswith("d")
            owner = bottom if up else 1 - bottom
            sq = (8 - row) * 9 + (8 - col) if flipped else row * 9 + col
            cells[sq] = sym if owner == 0 else sym.lower()
    hands = {0: {}, 1: {}}
    turn, turn_note = 0, "直前の手が分からなかったので、先手番にしてあります。違うときは「手番切替」を押してください"
    lm = data.get("last_move")
    if isinstance(lm, dict):
        try:
            r, c = int(lm["row"]) - 1, int(lm["col"]) - 1
            sq = (8 - r) * 9 + (8 - c) if flipped else r * 9 + c
            sym = cells[sq]
            if sym:
                turn = 1 if sym.isupper() else 0   # 直前に指した側の、次の手番
                turn_note = f"直前の手の位置から、{'先手' if turn == 0 else '後手'}番にしました"
        except (KeyError, TypeError, ValueError, IndexError):
            pass
    if lm is None and data.get("default_turn") == "bottom":
        turn = bottom
        turn_note = ("直前の手の印(赤いマス)が見つからなかったので、画面の下側の人の手番にしました。"
                     "違うときは「手番切替」を押してください")
    hin = data.get("hands") or {}
    for side, color in (("bottom", bottom), ("top", 1 - bottom)):
        for ch, n in (hin.get(side) or {}).items():
            sym = CHAR_TO_SYM.get(str(ch)[:1])
            try:
                n = int(n)
            except (TypeError, ValueError):
                n = 0
            if sym is None or sym.lstrip("+") == "K" or n <= 0:
                problems.append(f"持ち駒「{ch}」を読めませんでした")
                continue
            pt = BASE_PT[sym.lstrip("+")]
            hands[color][pt] = hands[color].get(pt, 0) + n
    return cells, hands, problems, turn, turn_note


def _json_from_text(text):
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.index("{"):] if "{" in t else t
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j < 0:
        raise ValueError("Geminiの回答からJSONを取り出せませんでした")
    return json.loads(t[i:j + 1])


def read_screenshot_kiou(png_bytes):
    """棋桜のスクショをOpenCVで読み取る。Gemini不要。{"sfen","messages","raw"} を返す。"""
    import cv2
    import numpy as np
    import kiou
    img = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("画像を開けませんでした")
    data, notes = kiou.read_board_image(img)
    data["default_turn"] = "bottom"      # 直前の手が分からないときは、画面の下側（自分）の手番とみなす
    cells, hands, problems, turn, turn_note = model_from_screenshot_json(data)
    messages = [("warn", n) for n in notes] + [("error", p) for p in problems] + validate_model(cells, hands) \
        + [("info", turn_note)]
    sfen = model_to_sfen(cells, hands, turn)
    shogi.Board(sfen)
    return {"sfen": sfen, "messages": messages, "raw": json.dumps(data, ensure_ascii=False)}


def read_screenshot(png_bytes, cfg):
    """スクショ(PNG)を読み取り、{"sfen","messages","raw"} を返す。手番は直前の手(赤いマス)から決める。
    cfg["screenshot_mode"] が "gemini" のときだけGeminiを使い、それ以外は棋桜専用のOpenCV読み取り。"""
    if cfg.get("screenshot_mode", "kiou") != "gemini":
        return read_screenshot_kiou(png_bytes)
    text = ask_gemini_image(SHOT_PROMPT, png_bytes, cfg.get("gemini_api_key", ""), cfg.get("gemini_model", ""))
    cells, hands, problems, turn, turn_note = model_from_screenshot_json(_json_from_text(text))
    messages = [("error", p) for p in problems] + validate_model(cells, hands) + [("info", turn_note)]
    sfen = model_to_sfen(cells, hands, turn)
    shogi.Board(sfen)  # 形式の確認（おかしければ例外）
    return {"sfen": sfen, "messages": messages, "raw": text}
