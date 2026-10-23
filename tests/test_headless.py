"""画面なしの検査。
blender -b --factory-startup --python tests/test_headless.py
"""
import os
import sys
import types

import bpy
import bmesh
from mathutils import Vector
from mathutils.bvhtree import BVHTree

ADDON_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ADDON_PARENT)
import proportional_surface_slide as pss  # noqa: E402
from proportional_surface_slide import core, ui_text  # noqa: E402

pss.register()
_OP = bpy.ops.mesh.surface_slide_proportional
failures = []


def OP(*a, **kw):
    """ERROR の報告は bpy.ops では例外になる。止まったことを {'CANCELLED'} として受け取る。"""
    try:
        return _OP(*a, **kw)
    except RuntimeError as e:
        print("     (報告)", str(e)[:80])
        return {'CANCELLED'}


def check(cond, msg):
    print(("OK   " if cond else "NG   ") + msg)
    if not cond:
        failures.append(msg)


def fresh():
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    for m in list(bpy.data.meshes):
        bpy.data.meshes.remove(m)


def surface_tree(coords, me):
    return BVHTree.FromPolygons(coords, [list(p.vertices) for p in me.polygons])


def max_off_surface(tree, coords):
    return max((tree.find_nearest(c)[0] - c).length for c in coords)


def edit_select(obj, pred):
    bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(obj.data)
    bm.select_mode = {'VERT'}
    bm.verts.ensure_lookup_table()
    ids = []
    for v in bm.verts:
        v.select = bool(pred(v))
        if v.select:
            ids.append(v.index)
    bm.select_flush_mode()
    bmesh.update_edit_mesh(obj.data)
    return ids


def edit_coords(obj):
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    return [v.co.copy() for v in bm.verts]


def set_edit_coords(obj, coords):
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    for v, c in zip(bm.verts, coords):
        v.co = c
    bmesh.update_edit_mesh(obj.data)


def sphere_with_keys(segments=48, rings=24, scale=1.0, keys=("open",)):
    fresh()
    bpy.ops.mesh.primitive_uv_sphere_add(segments=segments, ring_count=rings, radius=1.0)
    obj = bpy.context.object
    obj.scale = (scale, scale, scale)
    obj.shape_key_add(name="Basis")
    for k in keys:
        obj.shape_key_add(name=k)
    obj.active_shape_key_index = 1
    return obj


def slit_grid(n=20, half=0.65):
    """XZ 平面（-Y を向く＝正面）の格子に、z=0 の行の真ん中を切った口。"""
    fresh()
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=n, y_subdivisions=n, size=2.0, rotation=(1.5707963, 0, 0))
    obj = bpy.context.object
    bpy.ops.object.transform_apply(rotation=True)
    bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(obj.data)
    for f in bm.faces:
        if f.normal.y > 0:
            f.normal_flip()
    mid = [e for e in bm.edges if all(abs(v.co.z) < 1e-5 for v in e.verts) and all(abs(v.co.x) < half for v in e.verts)]
    bmesh.ops.split_edges(bm, edges=mid)
    bm.verts.index_update()
    bm.verts.ensure_lookup_table()
    upper, lower = [], []
    for v in bm.verts:
        if abs(v.co.z) < 1e-5 and abs(v.co.x) < half - 0.1:
            side = sum(f.calc_center_median().z for f in v.link_faces)
            (upper if side > 0 else lower).append(v.index)
    bmesh.update_edit_mesh(obj.data)
    bpy.ops.object.mode_set(mode='OBJECT')
    return obj, upper, lower


def mirror_index(start):
    out = []
    for p in start:
        out.append(min(range(len(start)), key=lambda k: (start[k] - Vector((-p.x, p.y, p.z))).length))
    return out


FRONT = dict(view_dir=(0.0, 1.0, 0.0), view_up=(0.0, 0.0, 1.0))

# ======================================================== 1. 球（拡大率 100）・シェイプキー
obj = sphere_with_keys(scale=100.0)
key = obj.data.shape_keys.key_blocks["open"]
basis = [v.co.copy() for v in obj.data.vertices]
sel = edit_select(obj, lambda v: abs(v.co.z) < 0.07 and v.co.y < -0.97)
check(len(sel) >= 2, f"選択 {len(sel)} 頂点")
r = OP(mode='MOVE', offset=(0, 0, 30.0), radius=60.0, falloff='SMOOTH', connected=True, snap=True, **FRONT)
check(r == {'FINISHED'}, f"実行できた {r}")
bpy.ops.object.mode_set(mode='OBJECT')
new = [p.co.copy() for p in key.data]
tree = surface_tree(basis, obj.data)
off = max_off_surface(tree, new)
check(off < 1e-4, f"全頂点が面の上（最大のずれ {off:.2e}・半径1）")
check(all((a - b).length < 1e-6 for a, b in zip(basis, [p.co for p in obj.data.shape_keys.reference_key.data])),
      "Basis は変わっていない")
moved_sel = [(new[i] - basis[i]).length for i in sel]
check(min(moved_sel) > 0.25, f"選択は動いた（ローカル {min(moved_sel):.3f}〜{max(moved_sel):.3f}、指示 0.30）")
check(all(new[i].z > basis[i].z for i in sel), "選択は上（+Z）へ動いた")
center = sum((basis[i] for i in sel), Vector()) / len(sel)
far = [i for i in range(len(basis)) if (basis[i] - center).length > 0.9]
check(all((new[i] - basis[i]).length < 1e-6 for i in far), "半径の外は動いていない")


def band(lo, hi):
    ids = [i for i in range(len(basis)) if lo <= (basis[i] - center).length < hi and i not in sel]
    return sum((new[i] - basis[i]).length for i in ids) / max(1, len(ids))


b1, b2, b3 = band(0.1, 0.2), band(0.25, 0.35), band(0.4, 0.5)
check(b1 > b2 > b3, f"減衰が距離とともに下がる（{b1:.3f} > {b2:.3f} > {b3:.3f}）")

for p, c in zip(key.data, basis):
    p.co = c
bpy.ops.object.mode_set(mode='EDIT')
OP(mode='MOVE', offset=(0, 0, 30.0), radius=60.0, **FRONT)
bpy.ops.object.mode_set(mode='OBJECT')
again = [p.co.copy() for p in key.data]
check(max((a - b).length for a, b in zip(new, again)) < 1e-6, "同じ値で 2 回目も同じ形（Redo と同じ道すじ）")

for p, c in zip(key.data, basis):
    p.co = c
bpy.ops.object.mode_set(mode='EDIT')
OP(mode='MOVE', offset=(0, 0, 30.0), radius=60.0, snap=False)
bpy.ops.object.mode_set(mode='OBJECT')
off2 = max_off_surface(tree, [p.co for p in key.data])
check(off2 > 1e-3, f"面に沿わせる OFF なら面から外れる（{off2:.3f}）＝ ON の効きが本物")

# ======================================================== 2. シェイプキーを壊さない
obj = sphere_with_keys(keys=("aa", "smile"))
obj.active_shape_key_index = 0
kb = obj.data.shape_keys.key_blocks
before = {k.name: [p.co.copy() for p in k.data] for k in kb}
edit_select(obj, lambda v: abs(v.co.z) < 0.1 and v.co.y < -0.95)
r = OP(offset=(0, 0, 0.2), radius=0.5)
bpy.ops.object.mode_set(mode='OBJECT')
diff = max((p.co - c).length for k in kb for p, c in zip(k.data, before[k.name]))
check(r == {'CANCELLED'} and diff < 1e-9, f"ベースがアクティブなら止める（{r}・差 {diff:.1e}）")
obj.active_shape_key_index = 1
kb["aa"].lock_shape = True
edit_select(obj, lambda v: abs(v.co.z) < 0.1 and v.co.y < -0.95)
r = OP(offset=(0, 0, 0.2), radius=0.5)
bpy.ops.object.mode_set(mode='OBJECT')
diff = max((p.co - c).length for k in kb for p, c in zip(k.data, before[k.name]))
check(r == {'CANCELLED'} and diff < 1e-9, f"ロック中のキーなら止める（{r}・差 {diff:.1e}）")
fresh()
bpy.ops.mesh.primitive_uv_sphere_add()
obj = bpy.context.object
obj.shape_key_add(name="Basis")
edit_select(obj, lambda v: abs(v.co.z) < 0.1 and v.co.y < -0.95)
r = OP(offset=(0, 0, 0.2), radius=0.5)
check(r == {'FINISHED'}, f"キーが Basis だけなら書き込む（{r}）")
fresh()
bpy.ops.mesh.primitive_uv_sphere_add()
obj = bpy.context.object
edit_select(obj, lambda v: False)
r = OP(offset=(0, 0, 0.2), radius=0.5)
check(r == {'CANCELLED'}, f"選択なしなら止める（{r}）")

# ======================================================== 3. 口の切れ目
obj, upper, lower = slit_grid()
check(len(upper) > 3 and len(lower) > 3, f"上唇 {len(upper)}・下唇 {len(lower)}")
ids = set(upper)
edit_select(obj, lambda v: v.index in ids)
start = edit_coords(obj)
OP(mode='MOVE', offset=(0, 0, 0.2), radius=0.5, connected=True, **FRONT)
co = edit_coords(obj)
lower_mid = [i for i in lower if abs(start[i].x) < 0.2]
lmove = max((co[i] - start[i]).length for i in lower_mid)
umove = min((co[i] - start[i]).length for i in upper)
yoff = max(abs(c.y) for c in co)
check(umove > 0.15, f"上唇が開いた（{umove:.3f}）")
check(lmove < 1e-6, f"つながった面だけ：下唇の真ん中は動かない（{lmove:.2e}）")
check(yoff < 1e-6, f"平面から浮かない（{yoff:.2e}）")
set_edit_coords(obj, start)
OP(mode='MOVE', offset=(0, 0, 0.2), radius=0.5, connected=False, **FRONT)
co = edit_coords(obj)
lmove2 = max((co[i] - start[i]).length for i in lower_mid)
check(lmove2 > 0.05, f"まっすぐの距離なら下唇の真ん中も動く（{lmove2:.3f}）")

# ======================================================== 4. 口を開く
set_edit_coords(obj, start)
both = set(upper) | set(lower)
corner = [i for i, c in enumerate(start) if abs(c.z) < 1e-5 and 0.55 < abs(c.x) < 0.65]
edit_select(obj, lambda v: v.index in both or v.index in corner)
r = OP(mode='OPEN', offset=(0, 0, -0.2), radius=0.6, upper_ratio=0.25, open_round=True, **FRONT)
co = edit_coords(obj)
check(r == {'FINISHED'}, f"口を開くが実行できた（{r}）")
lc = min(lower, key=lambda i: abs(start[i].x))
uc = min(upper, key=lambda i: abs(start[i].x))
gap = (co[uc].z - co[lc].z) - (start[uc].z - start[lc].z)
check(gap >= 0.8 * 0.2, f"口の真ん中のすき間が広がった（{gap:.3f}、ドラッグ 0.2）")
check(co[lc].z < start[lc].z - 0.15, f"下唇は下へ（{co[lc].z - start[lc].z:.3f}）")
check(0.02 < co[uc].z - start[uc].z < 0.08, f"上唇は 0.25 倍だけ上へ（{co[uc].z - start[uc].z:.3f}）")
cmove = max((co[i] - start[i]).length for i in corner) if corner else 0
check(corner and cmove < 1e-6, f"口角は動かない（{cmove:.1e}・{len(corner)} 個）")
side_l = max(lower, key=lambda i: abs(start[i].x))
check((co[side_l] - start[side_l]).length < (co[lc] - start[lc]).length * 0.8,
      "まるく開く：端の下唇は真ん中より動きが小さい")
check(max(abs(c.y) for c in co) < 1e-6, "口を開いても平面から浮かない")
set_edit_coords(obj, start)
OP(mode='OPEN', offset=(0, 0, -0.2), radius=0.6, open_round=False, **FRONT)
co = edit_coords(obj)
d_mid = (co[lc] - start[lc]).length
d_side = (co[side_l] - start[side_l]).length
check(abs(d_mid - d_side) < 0.02, f"まるく開く OFF：端も真ん中と同じだけ（{d_mid:.3f} / {d_side:.3f}）")
set_edit_coords(obj, start)
edit_select(obj, lambda v: v.index in corner)
r = OP(mode='OPEN', offset=(0, 0, -0.2), radius=0.6, **FRONT)
check(r == {'CANCELLED'}, f"上下に分けられないなら止める（{r}）")
bpy.ops.object.mode_set(mode='OBJECT')

# ======================================================== 5. 左右対称
fresh()
bpy.ops.mesh.primitive_grid_add(x_subdivisions=10, y_subdivisions=10, size=2.0)
obj = bpy.context.object
sel = edit_select(obj, lambda v: abs(v.co.x - 0.6) < 0.05 and abs(v.co.y) < 0.25)
start = edit_coords(obj)
mi = mirror_index(start)
OP(mode='MOVE', offset=(0, 0.2, 0), radius=0.5, use_mirror=True, snap=False)
co = edit_coords(obj)
dm = max((co[mi[i]] - start[mi[i]] - Vector((0, 0.2, 0))).length for i in sel)
check(dm < 1e-6, f"左右対称：反対側も同じだけ動く（差 {dm:.1e}）")
asym = max((Vector((-(co[i] - start[i]).x, (co[i] - start[i]).y, (co[i] - start[i]).z)) - (co[mi[i]] - start[mi[i]])).length
           for i in range(len(start)))
check(asym < 1e-6, f"左右対称：全頂点で鏡に写した動き（最大差 {asym:.1e}）")
set_edit_coords(obj, start)
OP(mode='MOVE', offset=(0.1, 0, 0), radius=0.5, use_mirror=True, snap=False)
co = edit_coords(obj)
dm = max((co[mi[i]] - start[mi[i]] - Vector((-0.1, 0, 0))).length for i in sel)
check(dm < 1e-6, f"左右対称：X に動かすと反対側は -X（差 {dm:.1e}）")
set_edit_coords(obj, start)
OP(mode='MOVE', offset=(0, 0.2, 0), radius=0.5, use_mirror=False, snap=False)
co = edit_coords(obj)
dm = max((co[mi[i]] - start[mi[i]]).length for i in sel)
check(dm < 1e-6, f"左右対称 OFF：反対側は動かない（{dm:.1e}）")
bpy.ops.object.mode_set(mode='OBJECT')

obj = sphere_with_keys()
sel = edit_select(obj, lambda v: v.co.x > 0.3 and abs(v.co.z) < 0.1 and v.co.y < -0.7)
start = edit_coords(obj)
mi = mirror_index(start)
OP(mode='MOVE', offset=(0, 0, 0.2), radius=0.4, use_mirror=True, snap=True, **FRONT)
co = edit_coords(obj)
asym = max((Vector((-(co[i] - start[i]).x, (co[i] - start[i]).y, (co[i] - start[i]).z)) - (co[mi[i]] - start[mi[i]])).length
           for i in range(len(start)))
moved = sum(1 for a, b in zip(co, start) if (a - b).length > 1e-6)
check(asym < 1e-5 and moved > 4, f"左右対称＋面に沿う（球）：鏡に写した動き（差 {asym:.1e}・{moved} 頂点）")
bpy.ops.object.mode_set(mode='OBJECT')

# ======================================================== 6. 軸の制限（Redo と同じ道すじ）
fresh()
bpy.ops.mesh.primitive_grid_add(x_subdivisions=10, y_subdivisions=10, size=2.0)
obj = bpy.context.object
sel = edit_select(obj, lambda v: abs(v.co.x) < 0.15 and abs(v.co.y) < 0.15)
start = edit_coords(obj)
OP(mode='MOVE', offset=(0.2, 0.3, 0), axis='X', radius=0.0, snap=False)
co = edit_coords(obj)
d = co[sel[0]] - start[sel[0]]
check((d - Vector((0.2, 0, 0))).length < 1e-6, f"軸 X：(0.2, 0.3, 0) は (0.2, 0, 0) になる（{tuple(round(x, 4) for x in d)}）")
set_edit_coords(obj, start)
OP(mode='MOVE', offset=(0.2, 0.3, 0.1), axis='Z', axis_plane=True, radius=0.0, snap=False)
co = edit_coords(obj)
d = co[sel[0]] - start[sel[0]]
check((d - Vector((0.2, 0.3, 0))).length < 1e-6, f"Z を除く：(0.2, 0.3, 0.1) は (0.2, 0.3, 0)（{tuple(round(x, 4) for x in d)}）")
bpy.ops.object.mode_set(mode='OBJECT')

# ======================================================== 7. 吸着：隠した面には乗らない・ベースの形は差分
obj = sphere_with_keys()
bpy.ops.object.mode_set(mode='EDIT')
bm = bmesh.from_edit_mesh(obj.data)
patch = [f for f in bm.faces if f.calc_center_median().y < -0.6]
ret = bmesh.ops.duplicate(bm, geom=patch)
dup_verts = [g for g in ret["geom"] if isinstance(g, bmesh.types.BMVert)]
for v in dup_verts:
    v.co *= 1.004
for g in ret["geom"]:
    g.hide = True
bmesh.update_edit_mesh(obj.data)
bm.verts.index_update()
dup_ids = {v.index for v in dup_verts}
edit_select(obj, lambda v: (not v.hide) and abs(v.co.z) < 0.07 and v.co.y < -0.97)
OP(mode='MOVE', offset=(0.6, 0, 0), radius=0.5, **FRONT)
co = edit_coords(obj)
worst = max(c.length for i, c in enumerate(co) if i not in dup_ids)
check(worst <= 1.0 + 1e-5, f"隠した面には乗らない（|p| の最大 {worst:.6f}）")
bpy.ops.object.mode_set(mode='OBJECT')

obj = sphere_with_keys()
k = obj.data.shape_keys.key_blocks["open"]
for p in k.data:
    p.co = p.co * 1.05
sel = edit_select(obj, lambda v: abs(v.co.z) < 0.08 and v.co.y < -1.0)
start = edit_coords(obj)
OP(mode='MOVE', offset=(0, 0, 0.001), radius=0.6, reference='BASIS', **FRONT)
co = edit_coords(obj)
jump = max((co[i] - start[i]).length for i in range(len(start)))
check(jump < 0.002, f"ベースの形：形の違うキーでも縁で跳ばない（最大の動き {jump:.4f}）")
bpy.ops.object.mode_set(mode='OBJECT')

# ======================================================== 8. 開始時の半径
fresh()
bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=0.1)
obj = bpy.context.object
edit_select(obj, lambda v: v.co.y < -0.0999)
bm = bmesh.from_edit_mesh(obj.data)
ctx = core.SlideContext(bm, obj.matrix_world)
rad, why = ctx.suggest_radius(1.0)
ctx.compute_weights(rad, 'SMOOTH', True)
check(why == 'BIG' and len(ctx.weights) < 0.25 * ctx.n, f"1 m は大きすぎ → {rad:.4f}（{len(ctx.weights)}/{ctx.n} 頂点）")
check(ctx.suggest_radius(0.03) == (0.03, None), "3 cm はそのまま")
check(ctx.suggest_radius(1.0, trusted=1.0) == (1.0, None), "自分で決めた半径はそのまま")
bpy.ops.object.mode_set(mode='OBJECT')
fresh()
bpy.ops.mesh.primitive_grid_add(x_subdivisions=10, y_subdivisions=10, size=0.1)
obj = bpy.context.object
edit_select(obj, lambda v: abs(v.co.x) < 0.006 and abs(v.co.y) < 0.006)
bm = bmesh.from_edit_mesh(obj.data)
ctx = core.SlideContext(bm, obj.matrix_world)
rad, why = ctx.suggest_radius(0.0005)
check(why == 'SMALL' and rad >= 0.05, f"0.5 mm は小さすぎ → {rad:.4f}")
bpy.ops.object.mode_set(mode='OBJECT')

# ======================================================== 9. 新しいキーを作る
fresh()
bpy.ops.mesh.primitive_uv_sphere_add()
obj = bpy.context.object
bpy.ops.object.mode_set(mode='EDIT')
r = bpy.ops.mesh.surface_slide_new_key('EXEC_DEFAULT', name="test_key")
keys = obj.data.shape_keys
check(r == {'FINISHED'} and keys is not None and obj.active_shape_key.name == "test_key"
      and obj.mode == 'EDIT' and len(keys.key_blocks) == 2,
      f"新しいキー：Basis と test_key ができて書き込み先になった（{[k.name for k in keys.key_blocks] if keys else None}）")
bpy.ops.object.mode_set(mode='OBJECT')

# ======================================================== 9b. 回転
import math  # noqa: E402

obj = sphere_with_keys(segments=64, rings=32)
basis = [v.co.copy() for v in obj.data.vertices]
sel = edit_select(obj, lambda v: v.co.y < -0.9 and abs(v.co.z) < 0.12 and abs(v.co.x) < 0.35)
r = OP(mode='ROTATE', angle=math.radians(90), radius=0.01, **FRONT)
check(r == {'FINISHED'} and len(sel) >= 6, f"回転：球で実行できた（{r}・{len(sel)} 頂点）")
co = edit_coords(obj)
bpy.ops.object.mode_set(mode='OBJECT')
off = max_off_surface(surface_tree(basis, obj.data), co)
check(off < 1e-4, f"回転：面の上に乗ったまま（最大のずれ {off:.1e}）")
c0 = sum((basis[i] for i in sel), Vector()) / len(sel)
das, ratios = [], []
for i in sel:
    a, b = basis[i] - c0, co[i] - c0
    if math.hypot(a.x, a.z) < 0.1:
        continue
    da = math.degrees(math.atan2(b.z, b.x) - math.atan2(a.z, a.x))
    das.append((da + 180) % 360 - 180)
    ratios.append(math.hypot(b.x, b.z) / math.hypot(a.x, a.z))
mean_da = sum(das) / len(das)
check(abs(mean_da - 90) < 5, f"回転：正面から見て左回りに 90°（平均 {mean_da:.1f}°・{len(das)} 頂点）")
check(min(ratios) > 0.9 and max(ratios) < 1.1, f"回転：中心からの距離はほぼそのまま（{min(ratios):.3f}〜{max(ratios):.3f}）")


def flat_grid(n=20):
    fresh()
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=n, y_subdivisions=n, size=2.0, rotation=(1.5707963, 0, 0))
    o = bpy.context.object
    bpy.ops.object.transform_apply(rotation=True)
    bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(o.data)
    for f in bm.faces:
        if f.normal.y > 0:
            f.normal_flip()
    bmesh.update_edit_mesh(o.data)
    bpy.ops.object.mode_set(mode='OBJECT')
    return o


obj = flat_grid()
sel = edit_select(obj, lambda v: abs(v.co.x) < 0.25 and abs(v.co.z) < 0.25)
start = edit_coords(obj)
OP(mode='ROTATE', angle=math.radians(120), radius=0.6, falloff='SMOOTH', **FRONT)
co = edit_coords(obj)
c0 = sum((start[i] for i in sel), Vector()) / len(sel)
moved = [i for i in range(len(start)) if (co[i] - start[i]).length > 1e-7]
dr = max(abs((co[i] - c0).length - (start[i] - c0).length) for i in moved)
dy = max(abs(co[i].y) for i in moved)
check(len(moved) > len(sel) and dr < 2e-3,
      f"回転 120°：まわりの頂点も弧の上を回り、中心へ縮まない（距離の差 最大 {dr:.1e}・{len(moved)} 頂点）")
check(dy < 1e-6, f"回転：平らな面から浮かない（{dy:.1e}）")
ang = []
for i in sel:
    a, b = start[i] - c0, co[i] - c0
    if a.length > 0.05:
        ang.append(((math.degrees(math.atan2(b.z, b.x) - math.atan2(a.z, a.x))) + 180) % 360 - 180)
check(all(abs(x - 120) < 0.5 for x in ang), f"回転：選んだ頂点はちょうど 120°（{min(ang):.2f}〜{max(ang):.2f}）")
snap_on = co
set_edit_coords(obj, start)
OP(mode='ROTATE', angle=math.radians(120), radius=0.6, falloff='SMOOTH', snap=False, **FRONT)
co = edit_coords(obj)
d = max((a - b).length for a, b in zip(co, snap_on))
check(d < 2e-3, f"回転：平らな面なら、面に沿わせても沿わせなくても同じ（差 {d:.1e}）")
set_edit_coords(obj, start)
OP(mode='ROTATE', angle=math.radians(120), radius=0.6, falloff='SMOOTH', **FRONT)
d = max((a - b).length for a, b in zip(edit_coords(obj), snap_on))
check(d < 1e-9, f"回転：同じ値で 2 回目も同じ形（F9 と同じ道すじ・差 {d:.1e}）")

# 左右対称：片側の眉を選んで回すと、反対側は逆向きに回る。両側を選んでも同じ
set_edit_coords(obj, start)
bpy.ops.object.mode_set(mode='OBJECT')
brow = edit_select(obj, lambda v: 0.35 < v.co.x < 0.75 and abs(v.co.z - 0.5) < 0.06)
OP(mode='ROTATE', angle=math.radians(20), radius=0.3, use_mirror=True, **FRONT)
co = edit_coords(obj)
mi = mirror_index(start)
asym = max((Vector((-(co[i] - start[i]).x, (co[i] - start[i]).y, (co[i] - start[i]).z)) - (co[mi[i]] - start[mi[i]])).length
           for i in range(len(start)))
c_src = sum((start[i] for i in brow), Vector()) / len(brow)
c_after = sum((co[i] for i in brow), Vector()) / len(brow)
check(asym < 1e-6, f"回転＋左右対称：反対側は鏡に写した動き＝逆向きの回転（最大差 {asym:.1e}）")
check((c_after - c_src).length < 1e-6, f"回転＋左右対称：中心は選んだ側の眉の真ん中（ずれ {(c_after - c_src).length:.1e}）")
one_side = co
set_edit_coords(obj, start)
bpy.ops.object.mode_set(mode='OBJECT')
edit_select(obj, lambda v: 0.35 < abs(v.co.x) < 0.75 and abs(v.co.z - 0.5) < 0.06)
OP(mode='ROTATE', angle=math.radians(20), radius=0.3, use_mirror=True, **FRONT)
d = max((a - b).length for a, b in zip(edit_coords(obj), one_side))
check(d < 1e-6, f"回転＋左右対称：両側の眉を選んでも、片側を選んだときと同じ（差 {d:.1e}）")
bpy.ops.object.mode_set(mode='OBJECT')

# ======================================================== 10. 文言
scene = bpy.context.scene
scene.unit_settings.system = 'METRIC'
scene.unit_settings.scale_length = 1.0
check("1.23 cm" in ui_text.fmt_len(scene, 0.0123), f"長さの表示：{ui_text.fmt_len(scene, 0.0123)!r}")


def fake(**kw):
    base = dict(mode='MOVE', num_text="", num_error=False, scale=1.0, offset_length=0.0123, axis='NONE',
                axis_space='GLOBAL', axis_plane=False, radius=0.02, falloff='SMOOTH', connected=True,
                affected=10, visible=100, use_mirror=False, open_info=None, upper_ratio=0.25, snap=True,
                reference='START', mirror_missing=0, hint="", angle=0.0, rot_view=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


fresh()
bpy.ops.mesh.primitive_uv_sphere_add()
obj = bpy.context.object
h = ui_text.header_text(fake(), scene, obj)
check("1.23 cm" in h and "書き込み先：メッシュ（シェイプキーなし）" in h, f"ヘッダー：{h}")
h = ui_text.header_text(fake(snap=False), scene, obj)
check("⚠ 面に沿わせていません" in h, "面に沿わせない警告")
icons = set(bpy.types.UILayout.bl_rna.functions['label'].parameters['icon'].enum_items.keys())
bad = [ic for it in ui_text.status_items(fake(), True) for ic in it[0] if ic not in icons]
check(not bad, f"ステータスバーのアイコンが全部ある（無い：{bad}）")
txt = " ".join(it[1] for it in ui_text.status_items(fake(snap=False), True))
check("面に沿わせる：OFF" in txt, "ステータスバーに状態が出る")
rna = _OP.get_rna_type()
hidden_ok = {'rna_type', 'use_center', 'view_dir', 'view_up', 'made_key', 'made_basis'}
empty = [p.identifier for p in rna.properties if p.identifier not in hidden_ok and not p.description]
check(not empty, f"プロパティに説明がある（無い：{empty}）")
check(pss.MESH_OT_surface_slide_proportional.bl_label == "面に沿ってスライド", "名前は「面に沿ってスライド」")

# ======================================================== 11. マニュアルとアドオンの食い違い
import html  # noqa: E402
import re  # noqa: E402

check(os.path.exists(pss.MANUAL_PATH), f"マニュアルがある（{pss.MANUAL_PATH}）")
man = open(pss.MANUAL_PATH, encoding="utf-8").read()
man_text = html.unescape(re.sub(r"<[^>]+>", "", man))
rows = re.findall(r'<td class="k">(.*?)</td><td>(.*?)</td><td class="(?:same|own)">(.*?)</td>', man)
rows = [tuple(html.unescape(c) for c in r) for r in rows]
check(rows == list(ui_text.MODAL_KEYS),
      f"操作中のキーの表が ui_text.MODAL_KEYS と同じ（マニュアル {len(rows)} 行 / {len(ui_text.MODAL_KEYS)} 行）")
for fid, name, desc, _icon, _n in core.FALLOFF_ITEMS:
    check(f"<b>{name}</b><span>{desc}</span>" in man, f"減衰「{name}」の説明がアドオンと同じ")
for key, frag in [('basis_active', "書き込み先がベース（Basis）なので止めました"),
                  ('no_selection', "動かしたい頂点を選んでから使ってください"),
                  ('open_failed', "上下に分けられませんでした"),
                  ('error', "エラーが起きたので元に戻しました"),
                  ('locked', "ロックされています")]:
    check(frag in ui_text.MSG[key] and frag in man_text, f"マニュアルの「{frag}」は実際のメッセージにある")
for frag in [ui_text.TOOL_NAME, ui_text.MENU_TEXT, ui_text.PANEL_TAB, "新しいキーを作って始める", "スライドを始める",
             "使い方を開く", "はじめの設定", "上唇の割合", "まるく開く", "つながった面だけ", "面に沿わせる",
             "沿わせる面", "左右対称（X）", "範囲の半径"]:
    check(frag in man_text, f"マニュアルに画面の文言「{frag}」がある")
imgs = re.findall(r'<img src="([^"]+)"', man)
missing = [p for p in imgs if not os.path.exists(os.path.join(os.path.dirname(pss.MANUAL_PATH), p))]
check(imgs and not missing, f"マニュアルの画像が全部ある（{len(imgs)} 枚・無い：{missing}）")
toml = open(os.path.join(os.path.dirname(pss.__file__), "blender_manifest.toml"), encoding="utf-8").read()
ver = re.search(r'^version\s*=\s*"([^"]+)"', toml, re.M).group(1)
check(f"proportional_surface_slide-{ver}.zip" in man and f"） {ver} ／" in man_text,
      f"マニュアルの版（zip 名・末尾）が manifest の {ver} と同じ")

pss.unregister()
print("\nRESULT:", "PASS" if not failures else f"FAIL {len(failures)}")
for f in failures:
    print("  NG:", f)
sys.exit(0 if not failures else 1)
