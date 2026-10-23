"""面に沿ってスライドの計算部分（UI なし。テストからも呼ぶ）。

座標の扱い：
- 距離（半径・減衰）はワールド空間で測る。オブジェクトの拡大率に左右されないように
- 面への吸着はローカル空間の BVHTree で行う（編集メッシュの座標がローカルなので）

吸着の考え方：
- 吸着してよい面＝隠れていない・選択（と左右の相手）がある島（つながったパーツ）・視点を向いている面
- そういう面に 1 枚も触れていない頂点（口の中・裏側・別のパーツ）は、面に沿わせずに動かす。
  口の中の縁は、自分が属する唇の動きについていく
"""
import heapq
import math

from mathutils import Quaternion, Vector
from mathutils.bvhtree import BVHTree
from mathutils.kdtree import KDTree

# (id, 名前, 説明, アイコン, 番号)
FALLOFF_ITEMS = [
    ('SMOOTH', "スムーズ", "なめらかに減る（迷ったらこれ）", 'SMOOTHCURVE', 0),
    ('SPHERE', "球状", "ふちの近くまでしっかり動き、ふちで止まる", 'SPHERECURVE', 1),
    ('ROOT', "ルート", "ふちの近くまでよくついてくる", 'ROOTCURVE', 2),
    ('INVERSE_SQUARE', "逆二乗", "球状より少しやわらかい", 'INVERSESQUARECURVE', 3),
    ('SHARP', "シャープ", "選択のすぐ近くだけ動く", 'SHARPCURVE', 4),
    ('LINEAR', "リニア", "距離に比例して減る", 'LINCURVE', 5),
    ('CONSTANT', "一定", "範囲の中は全部同じだけ動く（ふちに段差）", 'NOCURVE', 6),
]
FALLOFF_IDS = [f[0] for f in FALLOFF_ITEMS]
FALLOFF_NAMES = {f[0]: f[1] for f in FALLOFF_ITEMS}

# 吸着先の面の向きが、いまの向きとこれより食い違う面には乗らない（口の内側・裏面に落ちるのを防ぐ）
NORMAL_DOT_MIN = 0.3
# 視点を向いている面とみなす：面の法線・視線 < これ（横を向いた頬の面も入るように少し甘く）
FRONT_DOT_MAX = 0.25
# 口を開く：上唇／下唇の判定のしきい値
OPEN_SIDE_THRESHOLD = 0.25
MAX_STEPS = 64
# 回転を弧に沿わせるための 1 歩の角度の上限（ラジアン）
ROT_STEP_MAX = math.radians(5.0)


def falloff(kind, t):
    """t = 1 - 距離/半径（0..1）。Blender のプロポーショナル編集と同じ曲線。"""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    if kind == 'SPHERE':
        return math.sqrt(2.0 * t - t * t)
    if kind == 'ROOT':
        return math.sqrt(t)
    if kind == 'INVERSE_SQUARE':
        return t * (2.0 - t)
    if kind == 'SHARP':
        return t * t
    if kind == 'LINEAR':
        return t
    if kind == 'CONSTANT':
        return 1.0
    return 3.0 * t * t - 2.0 * t * t * t  # SMOOTH


def project_offset(offset, axis_vec, plane):
    """移動量を軸（線）か、軸を除いた平面に制限する。axis_vec が None なら何もしない。"""
    d = Vector(offset)
    if axis_vec is None:
        return d
    a = Vector(axis_vec).normalized()
    along = a * d.dot(a)
    return d - along if plane else along


def scale_rel(rel, s, axis_vec, plane):
    """中心からの位置 rel を倍率 s で広げた後の位置。軸・平面の制限つき。"""
    if axis_vec is None:
        return rel * s
    a = Vector(axis_vec).normalized()
    along = a * rel.dot(a)
    if plane:
        return rel + (rel - along) * (s - 1.0)
    return rel + along * (s - 1.0)


def _newell(coords, poly):
    n = Vector((0.0, 0.0, 0.0))
    k = len(poly)
    for j in range(k):
        p, q = coords[poly[j]], coords[poly[(j + 1) % k]]
        n.x += (p.y - q.y) * (p.z + q.z)
        n.y += (p.z - q.z) * (p.x + q.x)
        n.z += (p.x - q.x) * (p.y + q.y)
    return n


class SlideContext:
    """操作の開始時に一度だけ作る。開始時の座標・参照面・距離・左右の相手を持つ。

    bm             : 編集メッシュ（bmesh）
    matrix_world   : オブジェクトの行列
    basis_layer    : Basis のシェイプレイヤー（無ければ None）。左右の相手探しと「ベースの形」に使う
    use_basis      : True なら Basis の面に沿った動きを、いまの形に足す
    view_dir       : 視線の向き（ワールド、画面の奥向き）。None なら面の向きで絞らない
    view_up        : 画面の上向き（ワールド）。口を開くときの上下の判定に使う
    """

    def __init__(self, bm, matrix_world, basis_layer=None, use_basis=False,
                 view_dir=None, view_up=None):
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bm.verts.index_update()
        bm.faces.index_update()
        self.matrix_world = matrix_world.copy()
        self.mat3 = matrix_world.to_3x3()
        self.mat3_inv = self.mat3.inverted_safe()

        verts = bm.verts
        self.n = len(verts)
        self.start = [v.co.copy() for v in verts]
        self.start_world = [matrix_world @ c for c in self.start]
        self.hidden = [v.hide for v in verts]
        self.selected = [v.index for v in verts if v.select and not v.hide]
        self.neighbors = [[e.other_vert(v).index for e in v.link_edges if not e.other_vert(v).hide]
                          for v in verts]

        self.basis = [v[basis_layer].copy() for v in verts] if basis_layer is not None else None
        self.use_basis = bool(use_basis and self.basis is not None)
        self.ref = self.basis if self.use_basis else self.start

        # 視点（ローカル）
        self.view_dir = None
        if view_dir is not None and Vector(view_dir).length > 1e-9:
            self.view_dir = (self.mat3_inv @ Vector(view_dir)).normalized()
        up = Vector(view_up) if view_up is not None and Vector(view_up).length > 1e-9 else Vector((0.0, 0.0, 1.0))
        self.view_up_world = up.normalized()
        self.view_up = (self.mat3_inv @ up).normalized()
        vd = self.view_dir if self.view_dir is not None else (self.mat3_inv @ Vector((0.0, 1.0, 0.0))).normalized()
        self.view_right = vd.cross(self.view_up).normalized() if vd.cross(self.view_up).length > 1e-9 else Vector((1.0, 0.0, 0.0))

        # 面（参照面での法線・中心）
        self.polys = [[v.index for v in f.verts] for f in bm.faces]
        self.face_hidden = [f.hide for f in bm.faces]
        ref = self.ref
        self.face_normal = []
        self.face_center = []
        for poly in self.polys:
            nrm = _newell(ref, poly)
            self.face_normal.append(nrm.normalized() if nrm.length > 0 else Vector((0.0, 0.0, 1.0)))
            c = Vector((0.0, 0.0, 0.0))
            for vi in poly:
                c += ref[vi]
            self.face_center.append(c / max(1, len(poly)))
        self.link_faces = [[f.index for f in v.link_faces] for v in verts]

        # 島（つながったパーツ）
        self.island = [-1] * self.n
        k = 0
        for s in range(self.n):
            if self.island[s] >= 0:
                continue
            self.island[s] = k
            stack = [s]
            while stack:
                u = stack.pop()
                for e in verts[u].link_edges:
                    w = e.other_vert(verts[u]).index
                    if self.island[w] < 0:
                        self.island[w] = k
                        stack.append(w)
            k += 1
        self.island_count = k

        # 1歩の長さ：辺の平均の半分（ローカル）。選択につながる辺の平均（ワールド）は半径の目安に使う
        total, count = 0.0, 0
        for e in bm.edges:
            total += (e.verts[0].co - e.verts[1].co).length
            count += 1
        mean_edge = total / count if count else 1.0
        self.step_len = max(mean_edge * 0.5, 1e-9)
        sel_total, sel_count = 0.0, 0
        for i in self.selected:
            for j in self.neighbors[i]:
                sel_total += (self.start_world[i] - self.start_world[j]).length
                sel_count += 1
        self.sel_edge_world = sel_total / sel_count if sel_count else mean_edge * self.mat3.median_scale

        self.center_world = self._centroid_world(self.selected)

        # 左右の相手（ローカル X）
        self.mirror = None
        self.mirror_tol = max(1e-6, mean_edge * 0.01)

        self.weights = {}
        self.src = {}
        self.labels = {}
        self.open_info = None
        self.seeds = list(self.selected)
        self._tree_key = None
        self.tree = None
        self.allowed_face = None
        self.has_allowed = None

    # ---- 便利 ----

    def _centroid_world(self, ids):
        if not ids:
            return self.matrix_world.translation.copy()
        c = Vector((0.0, 0.0, 0.0))
        for i in ids:
            c += self.start_world[i]
        return c / len(ids)

    def selection_normal_world(self, ids=None, toward=None):
        """選んだ所の面の向き（ワールド）。toward（画面の手前向き）と同じ側にそろえる。"""
        ids = self.selected if ids is None else ids
        n = Vector((0.0, 0.0, 0.0))
        for i in ids:
            for fi in self.link_faces[i]:
                if not self.face_hidden[fi]:
                    n += self.face_normal[fi]
        nt = self.mat3_inv.transposed()
        w = nt @ n
        if w.length < 1e-12:
            w = Vector(toward) if toward is not None else Vector((0.0, 0.0, 1.0))
        w.normalize()
        if toward is not None and w.dot(Vector(toward)) < 0.0:
            w = -w
        return w

    def rotation_center(self, mirror=False):
        """回転の中心（ワールド）。左右対称なら元の側の選択だけで決める（両側を選んでも片側ずつ回るように）。"""
        ids = self.rotation_ids(mirror)
        return self._centroid_world(ids)

    def rotation_ids(self, mirror=False):
        if not mirror or not self.selected:
            return list(self.selected)
        sign = self._source_sign()
        ids = [i for i in self.selected if self.side(i) in (sign, 0)]
        return ids or list(self.selected)

    def _source_sign(self):
        xs = [(self.basis or self.start)[i].x for i in self.selected]
        mean = sum(xs) / len(xs) if xs else 0.0
        return -1 if mean < -self.mirror_tol else 1

    def selection_extent_world(self):
        if not self.selected:
            return 0.0
        lo = Vector(self.start_world[self.selected[0]])
        hi = Vector(lo)
        for i in self.selected:
            p = self.start_world[i]
            for a in range(3):
                lo[a] = min(lo[a], p[a])
                hi[a] = max(hi[a], p[a])
        return (hi - lo).length

    def suggest_radius(self, current, trusted=None):
        """開始時の半径を顔の大きさに合わせる。(半径, 理由) を返す。理由は None / 'BIG' / 'SMALL'。"""
        e = self.sel_edge_world
        auto = max(6.0 * e, 0.5 * self.selection_extent_world())
        if trusted is not None and trusted > 0 and current > 0 and abs(current - trusted) <= 1e-9 * max(1.0, abs(trusted)):
            return current, None
        if current > 10.0 * auto:
            return auto, 'BIG'
        if current < e:
            return auto, 'SMALL'
        return current, None

    # ---- 左右の相手 ----

    def build_mirror(self):
        if self.mirror is not None:
            return self.mirror
        coords = self.basis if self.basis is not None else self.start
        kd = KDTree(self.n)
        for i, c in enumerate(coords):
            kd.insert(c, i)
        kd.balance()
        tol = self.mirror_tol
        mirror = [-1] * self.n
        for i, c in enumerate(coords):
            if abs(c.x) < tol:
                mirror[i] = i
                continue
            co, j, d = kd.find(Vector((-c.x, c.y, c.z)))
            if j is not None and d <= tol:
                mirror[i] = j
        self.mirror = mirror
        return mirror

    def mirror_missing(self):
        """左右対称で、相手が見つからなかった選択頂点の数。"""
        m = self.build_mirror()
        return sum(1 for i in self.selected if m[i] < 0)

    def side(self, i):
        x = self.start[i].x if self.basis is None else self.basis[i].x
        if x > self.mirror_tol:
            return 1
        if x < -self.mirror_tol:
            return -1
        return 0

    # ---- 吸着してよい面 ----

    def _ensure_tree(self, seed_islands):
        key = frozenset(seed_islands)
        if key == self._tree_key:
            return
        self._tree_key = key
        allowed = []
        for fi, poly in enumerate(self.polys):
            ok = (not self.face_hidden[fi]) and self.island[poly[0]] in key
            if ok and self.view_dir is not None:
                ok = self.face_normal[fi].dot(self.view_dir) < FRONT_DOT_MAX
            allowed.append(ok)
        self.allowed_face = allowed
        self.has_allowed = [any(allowed[f] for f in lf) for lf in self.link_faces]
        polys = [p for p, ok in zip(self.polys, allowed) if ok]
        self.tree = BVHTree.FromPolygons(self.ref, polys, all_triangles=False, epsilon=0.0) if polys else None

    # ---- 範囲（重み） ----

    def compute_weights(self, radius, kind, connected, mirror=False, mode='MOVE', open_round=True):
        """重みを計算して self.weights（{頂点: 重み}）に入れる。

        選択（と左右の相手）は 1.0。まわりは距離に応じた重み。
        口を開くときは、上唇／下唇／口角に分け、口角は動かさず、口角を通る道も使わない。
        """
        seeds = list(self.selected)
        if mirror:
            m = self.build_mirror()
            extra = [m[i] for i in self.selected if m[i] >= 0]
            seeds = sorted(set(seeds) | set(extra))
        self.seeds = seeds
        self._ensure_tree({self.island[i] for i in seeds})

        anchors = set()
        base = {i: 1.0 for i in seeds}
        self.labels = {}
        self.open_info = None
        if mode == 'OPEN':
            labels = self.classify_open(seeds)
            self.labels = labels
            anchors = {i for i, l in labels.items() if l == 0}
            movers = [i for i, l in labels.items() if l != 0]
            upper = sum(1 for l in labels.values() if l < 0)
            lower = sum(1 for l in labels.values() if l > 0)
            self.open_info = {'upper': upper, 'lower': lower, 'corner': len(anchors)}
            if open_round and movers:
                base = self._open_profile(seeds, movers)
            seeds = movers

        dist, src = self._distances(seeds, radius, connected, anchors)
        weights = {}
        srcmap = {}
        seed_set = set(seeds)
        for i, d in dist.items():
            if i in anchors:
                continue
            b = base.get(src[i], 1.0)
            if i in seed_set:
                w = b
            elif radius <= 0.0:
                continue
            else:
                w = b * falloff(kind, 1.0 - d / radius)
            if w > 0.0:
                weights[i] = w
                srcmap[i] = src[i]
        self.weights = weights
        self.src = srcmap
        return weights

    def _distances(self, seeds, radius, connected, blocked):
        """種からの距離と、いちばん近い種。"""
        if not seeds:
            return {}, {}
        if connected:
            dist = {i: 0.0 for i in seeds}
            src = {i: i for i in seeds}
            heap = [(0.0, i) for i in seeds]
            heapq.heapify(heap)
            while heap:
                d, i = heapq.heappop(heap)
                if d > dist.get(i, math.inf):
                    continue
                pi = self.start_world[i]
                for j in self.neighbors[i]:
                    if j in blocked:
                        continue
                    nd = d + (self.start_world[j] - pi).length
                    if nd < radius and nd < dist.get(j, math.inf):
                        dist[j] = nd
                        src[j] = src[i]
                        heapq.heappush(heap, (nd, j))
            return dist, src
        kd = KDTree(len(seeds))
        for k, i in enumerate(seeds):
            kd.insert(self.start_world[i], k)
        kd.balance()
        dist, src = {}, {}
        for i, p in enumerate(self.start_world):
            if self.hidden[i] or i in blocked:
                continue
            _co, k, d = kd.find(p)
            if d is not None and d < radius:
                dist[i] = d
                src[i] = seeds[k]
        for i in seeds:
            dist[i] = 0.0
            src[i] = i
        return dist, src

    # ---- 口を開く ----

    def classify_open(self, ids):
        """+1＝下唇（面が下にある）、-1＝上唇、0＝口角・判断できない。視点を向いた面だけで見る。"""
        labels = {}
        vd = self.view_dir
        for i in ids:
            s, tot = 0.0, 0.0
            p = self.ref[i]
            for fi in self.link_faces[i]:
                if vd is not None and self.face_normal[fi].dot(vd) >= -0.2:
                    continue
                dz = (self.face_center[fi] - p).dot(self.view_up)
                s += dz
                tot += abs(dz)
            if tot == 0.0:
                labels[i] = 0
            elif s / tot < -OPEN_SIDE_THRESHOLD:
                labels[i] = 1
            elif s / tot > OPEN_SIDE_THRESHOLD:
                labels[i] = -1
            else:
                labels[i] = 0
        return labels

    def _open_profile(self, all_ids, movers):
        """口の幅の方向に放物線：真ん中 1、口角で 0。まるく開くための種ごとの強さ。

        市販アバターの顔メッシュで比べた（8 mm 開いたときの裏返った面）：楕円 sqrt(1-u²) 8 枚・口角にしわ、
        放物線 1-u² 2 枚・丸い「あ」、cos 0 枚だが V 字にとがる → 放物線にした。
        """
        r = self.view_right
        xs = [self.ref[i].dot(r) for i in all_ids]
        lo, hi = min(xs), max(xs)
        mid = 0.5 * (lo + hi)
        hw = 0.5 * (hi - lo) * 1.02
        if hw <= 1e-12:
            return {i: 1.0 for i in movers}
        out = {}
        for i in movers:
            u = min(1.0, abs(self.ref[i].dot(r) - mid) / hw)
            out[i] = max(0.0, 1.0 - u * u)
        return out

    # ---- 動かす量（ワールド） ----

    def displacement_world(self, i, mode, offset, scale, axis_vec, plane, upper_ratio, center):
        if mode == 'SCALE':
            rel = self.start_world[i] - center
            return scale_rel(rel, scale, axis_vec, plane) - rel
        d = Vector(offset)
        if mode == 'OPEN':
            src = self.src.get(i, i)
            if self.labels.get(src, 1) < 0:
                return d * (-upper_ratio)
        return d

    # ---- 面の上をすべらせる ----

    def _nearest_on_surface(self, pos, normal):
        loc, nrm, _idx, _d = self.tree.find_nearest(pos)
        if loc is None:
            return pos, normal
        if nrm.dot(normal) >= NORMAL_DOT_MIN:
            return loc, nrm
        # いちばん近い面が向きの違う面（口の内側など）→ 近くで向きの合う面を探す
        r = (loc - pos).length * 4.0 + self.step_len * 4.0
        best = None
        for hloc, hnrm, _hidx, hd in self.tree.find_nearest_range(pos, r):
            if hnrm.dot(normal) >= NORMAL_DOT_MIN and (best is None or hd < best[2]):
                best = (hloc, hnrm, hd)
        if best is None:
            return loc, nrm
        return best[0], best[1]

    def _start_normal(self, i, disp_local):
        """歩き始めの向き：進む方向にある、吸着してよい面の法線。"""
        base = self.ref[i]
        d = disp_local.normalized() if disp_local.length > 0 else Vector((0.0, 0.0, 0.0))
        normal = None
        best = -2.0
        for fi in self.link_faces[i]:
            if not self.allowed_face[fi]:
                continue
            v = self.face_center[fi] - base
            score = v.normalized().dot(d) if v.length > 0 else -1.0
            if score > best:
                best, normal = score, self.face_normal[fi]
        return normal if normal is not None else Vector((0.0, 0.0, 1.0))

    def slide(self, i, disp_local):
        """disp_local だけ、参照面の上を少しずつ歩かせて動かす。いまの形に足した位置を返す。"""
        length = disp_local.length
        if length == 0.0:
            return self.start[i].copy()
        if self.tree is None or not self.has_allowed[i]:
            return self.start[i] + disp_local
        base = self.ref[i]
        pos = base.copy()
        steps = min(MAX_STEPS, max(1, math.ceil(length / self.step_len)))
        step = disp_local / steps
        normal = self._start_normal(i, disp_local)
        for _ in range(steps):
            pos, normal = self._nearest_on_surface(pos + step, normal)
        return self.start[i] + (pos - base)

    def slide_rotate(self, i, center_world, axis_world, angle, snap=True):
        """center_world を通る axis_world のまわりに angle だけ回す。面に沿わせるときは角度を刻んで弧の上を歩く。"""
        if angle == 0.0:
            return self.start[i].copy()
        axis = Vector(axis_world).normalized()
        mw = self.matrix_world
        mw_inv = mw.inverted_safe()
        c = Vector(center_world)
        rot = Quaternion(axis, angle).to_matrix()
        if not snap or self.tree is None or not self.has_allowed[i]:
            pw = self.start_world[i]
            return self.start[i] + self.mat3_inv @ (rot @ (pw - c) + c - pw)
        base = self.ref[i]
        # 参照面がいまの形と違う（ベースの形）ときは、中心も同じだけずらす
        if self.use_basis:
            ids = self.selected
            shift = Vector((0.0, 0.0, 0.0))
            for k in ids:
                shift += self.basis[k] - self.start[k]
            c = c + self.mat3 @ (shift / max(1, len(ids)))
        pw0 = mw @ base
        total = self.mat3_inv @ (rot @ (pw0 - c) + c - pw0)
        steps = max(math.ceil(abs(angle) / ROT_STEP_MAX), math.ceil(total.length / self.step_len))
        steps = min(MAX_STEPS, max(1, steps))
        rstep = Quaternion(axis, angle / steps).to_matrix()
        normal = self._start_normal(i, total)
        pos = base.copy()
        for _ in range(steps):
            pw = mw @ pos
            target = mw_inv @ (rstep @ (pw - c) + c)
            pos, normal = self._nearest_on_surface(target, normal)
        return self.start[i] + (pos - base)

    def solve(self, mode='MOVE', offset=(0.0, 0.0, 0.0), scale=1.0, axis_vec=None, plane=False,
              snap=True, mirror=False, upper_ratio=0.25, center=None, angle=0.0, rot_axis=None):
        """{頂点: 新しいローカル座標} を返す。"""
        center = Vector(center) if center is not None else self.center_world
        if mode != 'SCALE':
            offset = project_offset(offset, axis_vec, plane)
        out = {}
        moved = {}  # 頂点 → 実際に動いた量（ローカル）。口の中が唇についていくのに使う

        m = self.build_mirror() if mirror else None
        src_sign = self._source_sign() if mirror else 1

        def solve_one(i, w):
            if mode == 'ROTATE':
                # 重みは角度にかける（回った位置との直線で混ぜると、中心側へ縮むため）
                return self.slide_rotate(i, center, rot_axis, angle * w, snap)
            dw = self.displacement_world(i, mode, offset, scale, axis_vec, plane, upper_ratio, center) * w
            dl = self.mat3_inv @ dw
            if snap:
                return self.slide(i, dl)
            return self.start[i] + dl

        # 1) 種と、面に触れている頂点（左右対称なら元の側と中心線）
        later = []
        for i, w in self.weights.items():
            if mirror and self.side(i) == -src_sign and m[i] >= 0 and m[i] in self.weights:
                continue  # 反対側：あとで写す
            if snap and self.tree is not None and not self.has_allowed[i] and i != self.src.get(i, i):
                later.append(i)  # 口の中など：唇の動きについていく
                continue
            out[i] = solve_one(i, w)
            moved[i] = out[i] - self.start[i]
        # 2) 口の中などは、自分の種の動き × 重み
        for i in later:
            s = self.src.get(i, i)
            ws = self.weights.get(s, 1.0) or 1.0
            if s in moved:
                out[i] = self.start[i] + moved[s] * (self.weights[i] / ws)
            else:
                out[i] = solve_one(i, self.weights[i])
            moved[i] = out[i] - self.start[i]
        # 3) 中心線は X を動かさない
        if mirror:
            for i in list(out.keys()):
                if self.side(i) == 0:
                    out[i].x = self.start[i].x
        # 4) 反対側は相手の動きを鏡に写す
        if mirror:
            for i, w in self.weights.items():
                if i in out:
                    continue
                p = m[i]
                if p >= 0 and p in out:
                    d = out[p] - self.start[p]
                    out[i] = self.start[i] + Vector((-d.x, d.y, d.z))
                else:
                    out[i] = solve_one(i, w)
        return out


def apply_positions(bm, ctx, positions, touched_before=()):
    """前回動かしたのに今回は対象外になった頂点を戻してから、新しい位置を入れる。"""
    for i in touched_before:
        if i not in positions:
            bm.verts[i].co = ctx.start[i]
    for i, co in positions.items():
        bm.verts[i].co = co
