"""棋桜(Steam)のスクリーンショットから局面を読み取る(OpenCVのテンプレート照合)。
結果は core.model_from_screenshot_json が読めるJSON形式の辞書で返す。"""
import os
import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_FILE = os.path.join(HERE, "kiou_templates.npz")
CELL_W, CELL_H = 79.0, 87.0
TOTAL = {"歩": 18, "香": 4, "桂": 4, "銀": 4, "金": 4, "角": 2, "飛": 2}
PROMO_BASE = {"と": "歩", "杏": "香", "圭": "桂", "全": "銀", "馬": "角", "龍": "飛"}


def _norm(ch):
    return "玉" if ch and ch in "玉王" else ch


# ---------------------------------------------------------------- 盤の位置
def _best_progression(score, smin, smax, step_size=0.25):
    """10本の等間隔の線の位置(開始,間隔)を、線らしさの合計が最大になるように探す。"""
    n = len(score)
    cs = np.convolve(score, np.ones(5), "same")
    best = (-1.0, 0, 0)
    k = np.arange(10)
    for step in np.arange(smin, smax, step_size):
        top = int(n - 9 * step - 1)
        if top <= 0:
            break
        starts = np.arange(0, top)
        idx = np.rint(starts[:, None] + step * k[None, :]).astype(int)
        sel = cs[idx]
        v = sel.sum(axis=1) + 4 * sel.min(axis=1)       # 10本とも強い線であることを重視する
        j = int(v.argmax())
        if v[j] > best[0]:
            best = (float(v[j]), int(starts[j]), float(step))
    return best


def find_grid(img):
    """盤の升目の線から (左端x, 上端y, 升の幅, 升の高さ) を求める。画面の大きさが違っても見つかる。"""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    dark = (g < 120).astype(np.uint8) * 255
    hl = cv2.morphologyEx(dark, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (150, 1)))
    vl = cv2.morphologyEx(dark, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 150)))
    rows = (hl > 0).sum(axis=1).astype(float)
    cols = (vl > 0).sum(axis=0).astype(float)
    ratio = CELL_H / CELL_W
    best = None
    for sx in np.arange(40, 170, 0.5):                      # 升の幅を変えながら、縦線と横線の合計点が最大の組を探す
        vx, x0, _ = _best_progression(cols, sx, sx + 0.01)
        vy, y0, sy = _best_progression(rows, sx * ratio * 0.96, sx * ratio * 1.04, 0.25)
        if best is None or vx + vy > best[0]:
            best = (vx + vy, x0, y0, sx, sy)
    _, x0, y0, sx, sy = best
    return float(x0), float(y0), float(sx), float(sy)


# ---------------------------------------------------------------- 特徴量
def _ink(region):
    """黒い字と赤い字(成駒)を別々の層にして返す。"""
    i = region.astype(int)
    B, G, R = i[..., 0], i[..., 1], i[..., 2]
    black = i.max(2) < 90
    red = (~black) & (R <= 200) & (R > G + 35) & (G < 100) & (B < 100)
    return black.astype(np.float32), red.astype(np.float32)


def ink_map(cell):
    layers = []
    for ink in _ink(cell[6:-6, 6:-6]):
        ink = cv2.resize(ink, (32, 36), interpolation=cv2.INTER_AREA)
        layers.append(cv2.GaussianBlur(ink, (3, 3), 0.8))
    return np.stack(layers)          # 形: (2, 36, 32)


def _cos(a, b):
    a = a.ravel() - a.mean()
    b = b.ravel() - b.mean()
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def cell_at(img, grid, r, c):
    x0, y0, sx, sy = grid
    return img[int(round(y0 + r * sy)):int(round(y0 + (r + 1) * sy)),
               int(round(x0 + c * sx)):int(round(x0 + (c + 1) * sx))]


def _red_score(cell):
    """升の外周がどれだけ赤(直前の手の印)か。駒に隠れにくい外周だけを見る。"""
    cl = cell.astype(int)
    ring = np.concatenate([cl[:5].reshape(-1, 3), cl[-5:].reshape(-1, 3)])
    m = (ring[:, 2] > 225) & (ring[:, 1] < 135) & (ring[:, 0] < 95)
    return float(m.mean())


# ---------------------------------------------------------------- 持ち駒
def _yellow(region):
    i = region.astype(int)
    B, G, R = i[..., 0], i[..., 1], i[..., 2]
    return ((R > 215) & (G > 130) & (G < 235) & (B < 120)).astype(np.uint8)


def hand_pieces(img, grid):
    """持ち駒の駒を探し、[(side, x, y, w, h)] を返す。side は 'top' か 'bottom'。"""
    x0, y0, sx, sy = grid
    H, W = img.shape[:2]
    bx1 = int(x0 + 9 * sx)
    regions = {"top": (0, int(x0 - 15), 0, int(y0 + 175)),
               "bottom": (bx1 + 25, W, int(y0 + 620), H)}
    out = []
    for side, (xa, xb, ya, yb) in regions.items():
        if xb - xa < 60 or yb - ya < 60 or xa < 0:
            continue
        m = cv2.morphologyEx(_yellow(img[ya:yb, xa:xb]), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        n, lab, st, cen = cv2.connectedComponentsWithStats(m)
        for s in st[1:]:
            x, y, w, h, a = [int(v) for v in s]
            if a > 1500 and 45 <= w <= 85 and 55 <= h <= 90:
                out.append((side, x + xa, y + ya, w, h))
    return out


def hand_map(img, grid, box, scale=1.0):
    """持ち駒1枚を、盤上の1升と同じ枠に収めた特徴量にする。"""
    _, x, y, w, h = box
    cx, cy = x + w / 2, y + h / 2
    cw, ch = CELL_W / scale, CELL_H / scale
    xa, ya = int(round(cx - cw / 2)), int(round(cy - ch / 2))
    pad = 40
    big = cv2.copyMakeBorder(img, pad, pad, pad, pad, cv2.BORDER_REPLICATE)
    crop = big[ya + pad:ya + pad + int(round(ch)), xa + pad:xa + pad + int(round(cw))]
    crop = cv2.resize(crop, (int(CELL_W), int(CELL_H)))
    return ink_map(crop)


def digit_glyph(img, box):
    """持ち駒の右側にある白い数字を切り出して正規化する。無ければ None(=1枚)。"""
    _, x, y, w, h = box
    xa, xb, ya, yb = x + w - 26, x + w + 36, max(y - 20, 0), y + h + 20
    reg = img[ya:yb, max(xa, 0):xb]
    m = (reg.min(axis=2) > 215).astype(np.uint8)
    n, lab, st, cen = cv2.connectedComponentsWithStats(m)
    best = None
    for s in st[1:]:
        sx_, sy_, sw, sh, a = [int(v) for v in s]
        if 20 <= sh <= 34 and 8 <= sw <= 26 and a > 60:
            if best is None or a > best[4]:
                best = (sx_, sy_, sw, sh, a)
    if best is None:
        return None
    sx_, sy_, sw, sh, _ = best
    g = m[sy_:sy_ + sh, sx_:sx_ + sw].astype(np.float32)
    return cv2.GaussianBlur(cv2.resize(g, (16, 24), interpolation=cv2.INTER_AREA), (3, 3), 0.7)


def read_digit(glyph, T):
    sc = sorted(((_cos(glyph, t), int(k[6:])) for k, t in T.items() if k.startswith("digit_")), reverse=True)
    if not sc:
        return None, 0.0
    return sc[0][1], sc[0][0]


# ---------------------------------------------------------------- 先手・後手の表示
def _plate_band(img, grid, extra=0):
    x0, y0, sx, sy = grid
    H, W = img.shape[:2]
    bx1 = int(x0 + 9 * sx)
    ya = int(y0 + 503) - extra
    b = img[max(ya, 0):int(y0 + 548) + extra, bx1:min(W, bx1 + 330)]
    return (b.min(2) > 150).astype(np.float32)


def _plate_crop(m, dx, w=44, dy=0, h=45):
    return cv2.GaussianBlur(m[dy:dy + h, dx:dx + w], (3, 3), 0.8)


def plate_template(img, grid, dx):
    return _plate_crop(_plate_band(img, grid), dx)


def read_plate(img, grid, T):
    """画面下側のプレートを読み、('先'|'後', 確からしさ) を返す。位置が数ピクセルずれても探す。"""
    m = _plate_band(img, grid, extra=8)
    best = (-1, None)
    for lab in ("先", "後"):
        t = T["plate_" + lab]
        for dy in range(0, 17, 2):
            for dx in range(100, min(m.shape[1] - 44, 260)):
                if m.shape[0] < dy + 45:
                    continue
                s = _cos(_plate_crop(m, dx, dy=dy), t)
                if s > best[0]:
                    best = (s, lab)
    return best[1], best[0]


# ---------------------------------------------------------------- テンプレート
def load_templates(path=TEMPLATE_FILE):
    z = np.load(path)
    return {k: z[k] for k in z.files}


def with_rotations(T):
    """反対向きの見本は180度回転で補う。"""
    T = dict(T)
    for k in list(T):
        if k.startswith(("plate_", "digit_")):
            continue
        pre = "hand_" if k.startswith("hand_") else ""
        t, d = k[len(pre):-1], k[-1]
        o = pre + t + ("u" if d == "d" else "d")
        if o not in T:
            T[o] = np.rot90(T[k], 2, axes=(-2, -1)).copy()
    return T


def classify(m, T):
    if m[0].sum() + m[1].sum() < 0.03 * m[0].size:
        return None, 1.0, 1.0
    sc = sorted(((_cos(m, t), k) for k, t in T.items() if not k.startswith(("plate_", "digit_", "hand_"))), reverse=True)
    return sc[0][1], sc[0][0] - sc[1][0], sc[0][0]


# ---------------------------------------------------------------- 読み取り本体
def read_board_image(img, T=None, min_margin=0.02):
    """画像(BGR)を読み、core.model_from_screenshot_json 用の辞書と注意書きを返す。"""
    T = with_rotations(T or load_templates())
    grid = find_grid(img)
    if abs(grid[2] - CELL_W) > 1.0 and grid[2] > 0:      # 画面の大きさが違うときは、見本と同じ大きさに縮尺をそろえる
        k = CELL_W / grid[2]
        img = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_CUBIC)
        grid = find_grid(img)
    notes = []
    cells = [[""] * 9 for _ in range(9)]
    last, red_best = None, 0.0
    scores = []
    for r in range(9):
        for c in range(9):
            cl = cell_at(img, grid, r, c)
            if cl.size == 0:
                continue
            rs = _red_score(cl)
            if rs > 0.08 and rs > red_best:
                red_best, last = rs, {"row": r + 1, "col": c + 1}
            k, mg, best = classify(ink_map(cl), T)
            if k:
                scores.append(best)
                cells[r][c] = k
                if mg < min_margin:
                    notes.append(f"{r + 1}行目・{c + 1}列目の「{k[:-1]}」は自信がありません(差 {mg:.2f})")
    if not (6 <= len(scores) <= 42) or float(np.median(scores)) < 0.65:
        raise ValueError("棋桜の対局画面として読み取れませんでした（盤が見つからないか、別のゲーム・サイトの画面です）。"
                         "この読み取りは棋桜専用です。")
    lab, sc = read_plate(img, grid, T)
    if sc < 0.6:
        notes.append("先手・後手の表示を読めませんでした。盤の向きは先手が下として扱います")
        flipped = False
    else:
        flipped = (lab == "後")
    # 持ち駒
    boxes = hand_pieces(img, grid)
    held = []
    if not boxes:
        notes.append("持ち駒が画面に見つかりませんでした。持ち駒なしとして読んでいます。ある場合は編集モードで足してください")
    for b in boxes:
        d = "d" if b[0] == "top" else "u"     # 画面の上側の持ち駒は逆向き、下側は正向き
        m = hand_map(img, grid, b)
        sc = sorted(((_cos(m, t), k[5:]) for k, t in T.items() if k.startswith("hand_") and k[-1] == d), reverse=True)
        if len(sc) < 2:
            continue
        mg = sc[0][0] - sc[1][0]
        if mg < 0.01:
            notes.append(f"持ち駒の「{sc[0][1][:-1]}」は自信がありません(差 {mg:.2f})")
        held.append((b[0], sc[0][1][:-1], digit_glyph(img, b)))
    board_count = {}
    for row in cells:
        for p in row:
            if p:
                ch = p[:-1]
                base = PROMO_BASE.get(ch, _norm(ch))
                board_count[base] = board_count.get(base, 0) + 1
    hands = {"top": {}, "bottom": {}}
    digit_of = {}                                  # (side, 駒) -> 読めた枚数 / None=読めない
    for side, ch, g in held:
        if g is None:
            digit_of[(side, ch)] = 1
        else:
            n, sc = read_digit(g, T)
            digit_of[(side, ch)] = n if (n and sc > 0.75) else None
    for ch in sorted({c for _, c, _ in held}):
        if ch not in TOTAL:
            notes.append(f"持ち駒に「{ch}」が見えましたが、持ち駒にならない駒です")
            continue
        missing = TOTAL[ch] - board_count.get(ch, 0)
        sides = [s_ for s_ in ("top", "bottom") if (s_, ch) in digit_of]
        vals = {s_: digit_of[(s_, ch)] for s_ in sides}
        unknown = [s_ for s_, v in vals.items() if v is None]
        if len(unknown) == 1:                       # 1か所だけ読めないときは、残り枚数から割り出す
            vals[unknown[0]] = missing - sum(v for v in vals.values() if v is not None)
        elif len(unknown) == 2:
            notes.append(f"持ち駒「{ch}」の枚数の数字を読めませんでした。編集モードで確認してください")
            for s_ in unknown:
                vals[s_] = 1
        total = sum(vals.values())
        if total != missing or any(v < 1 for v in vals.values()):
            notes.append(f"持ち駒「{ch}」の枚数が合いません(盤上と合わせて{TOTAL[ch]}枚になるはずです)。確認してください")
            for s_ in sides:
                vals[s_] = max(1, vals[s_])
        for s_ in sides:
            hands[s_][ch] = vals[s_]
    data = {"cells": cells, "flipped": flipped, "hands": hands, "last_move": last}
    return data, notes
