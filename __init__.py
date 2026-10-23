"""面に沿ってスライド（プロポーショナル）。

編集モードで、選んだ頂点を「面から浮かせず・めり込ませず」すべらせ、まわりの頂点も
プロポーショナル編集の減衰で一緒にすべらせる。表情シェイプキー（口を開ける・口角を上げる）向け。

- 操作は Blender の G（プロポーショナル編集つき）にそろえてある。違うのは F・B・M・A のキーだけ
- 書き込み先は編集モードのアクティブなシェイプキー。ベース（Basis）・ロック中のキーには書かない
- 使い方は docs/manual.html（N パネルの「使い方を開く」）
"""
import math
import os
import traceback

import bpy
import blf
import bmesh
import gpu
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty,
                       PointerProperty, StringProperty)
from bpy_extras import view3d_utils
from gpu_extras.batch import batch_for_shader
from mathutils import Vector, geometry

from . import core
from . import ui_text as T

MANUAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "manual.html")
OP_IDNAME = "mesh.surface_slide_proportional"

MODE_ITEMS = [
    ('MOVE', "移動", "マウスの動きのぶん、面に沿って動かす", 'ORIENTATION_VIEW', 0),
    ('SCALE', "拡大縮小", "選択の中心から広げる・縮める", 'FULLSCREEN_ENTER', 1),
    ('OPEN', "口を開く", "唇の切れ目を上下に分け、下唇はマウスについて、上唇は少し反対へ動かす", 'MESH_UVSPHERE', 2),
]
AXIS_ITEMS = [
    ('NONE', "なし", "すべての方向"),
    ('X', "X", "X 方向だけ"),
    ('Y', "Y", "Y 方向だけ"),
    ('Z', "Z", "Z 方向だけ（上下）"),
]
SPACE_ITEMS = [('GLOBAL', "グローバル", "ワールドの軸"), ('LOCAL', "ローカル", "オブジェクトの軸")]
REF_ITEMS = [
    ('START', "いまの形", "始めたときの形の上をすべらせる（ふつうはこれ）"),
    ('BASIS', "ベースの形", "ベース（Basis）の面に沿った向きで動かす。キーを重ねて形がずれてきたとき向け"),
]
FALLOFF_ENUM = [tuple(f) for f in core.FALLOFF_ITEMS]

D_RADIUS = "この距離までの頂点がついてくる。Blender のプロポーショナルサイズと共通。操作中はホイールで変える"
D_CONNECTED = ("メッシュをたどった距離で範囲を決める。上唇を動かしても、切れ目の向こうの下唇は動かない"
               "（Blender の「接続のみ」と同じ考え方）。オフにするとまっすぐの距離")
D_SNAP = "オン：面から浮かず、めり込まない。オフ：ふつうのプロポーショナル移動と同じ"
D_MIRROR = "反対側も鏡に写したように動かす（ローカル X）。はじめはメッシュの X ミラー設定と同じ"
D_UPPER = "口を開くとき、下唇はマウスについて行き、上唇はこの割合だけ反対へ動く（0 なら上唇は動かない）"
D_ROUND = "口の真ん中ほど大きく開き、口角に向かって小さくなる。オフにすると全体が同じだけ開く（四角い口）"

_draw_handles = []
_keymaps = []
_live = None  # いま動いているオペレーター（描画とステータスバー用）


# ---------------------------------------------------------------- 設定

def _radius_get(self):
    return self.id_data.tool_settings.proportional_size


def _radius_set(self, value):
    self.id_data.tool_settings.proportional_size = value
    self.last_radius = value


class SurfaceSlideSettings(bpy.types.PropertyGroup):
    """N パネルの「はじめの設定」。操作中のキーで変えた分は、その回だけ。"""
    connected: BoolProperty(name="つながった面だけ", default=True, description=D_CONNECTED)
    snap: BoolProperty(name="面に沿わせる", default=True, description=D_SNAP)
    reference: EnumProperty(name="沿わせる面", items=REF_ITEMS, default='START',
                            description="どの形の面に沿ってすべらせるか")
    upper_ratio: FloatProperty(name="上唇の割合", default=0.25, min=0.0, max=1.0, description=D_UPPER)
    open_round: BoolProperty(name="まるく開く", default=True, description=D_ROUND)
    radius: FloatProperty(name="範囲の半径", get=_radius_get, set=_radius_set, min=0.0,
                          subtype='DISTANCE', unit='LENGTH', description=D_RADIUS)
    last_radius: FloatProperty(default=-1.0, options={'HIDDEN'})


def _settings(context):
    return context.scene.surface_slide


# ---------------------------------------------------------------- 共通

def _check_target(obj):
    """書いてはいけないキーなら (level, 文言)。"""
    keys = obj.data.shape_keys
    if keys is not None and len(keys.key_blocks) > 1 and obj.active_shape_key_index == 0:
        return 'ERROR', T.MSG['basis_active']
    kb = obj.active_shape_key
    if kb is not None and kb.lock_shape:
        return 'ERROR', T.MSG['locked'].format(key=kb.name)
    return None


def _has_keys(obj):
    keys = obj.data.shape_keys
    return keys is not None and len(keys.key_blocks) > 1 and obj.active_shape_key_index != 0


def _window_region(context):
    area = context.area
    if area is None or area.type != 'VIEW_3D':
        return None, None, None
    region = context.region
    if region is None or region.type != 'WINDOW':
        region = next((r for r in area.regions if r.type == 'WINDOW'), None)
    rv3d = context.region_data if (context.region is region and context.region_data) else area.spaces.active.region_3d
    return area, region, rv3d


def _axis_vec(op, obj):
    if op.axis == 'NONE':
        return None
    k = 'XYZ'.index(op.axis)
    if op.axis_space == 'LOCAL':
        col = obj.matrix_world.to_3x3().col[k].copy()
        return col.normalized() if col.length > 0 else None
    v = Vector((0.0, 0.0, 0.0))
    v[k] = 1.0
    return v


def _kmi_text():
    kc = bpy.context.window_manager.keyconfigs.user
    km = kc.keymaps.get("Mesh") if kc else None
    if km is None:
        return "Shift Alt V"
    for kmi in km.keymap_items:
        if kmi.idname == OP_IDNAME and kmi.active:
            try:
                return kmi.to_string()
            except Exception:  # noqa: BLE001
                return "Shift Alt V"
    return "なし（キーマップで無効）"


# ---------------------------------------------------------------- 本体

class MESH_OT_surface_slide_proportional(bpy.types.Operator):
    """選んだ頂点を、面から浮かせず・めり込ませずにすべらせます。まわりの頂点もプロポーショナル編集の減衰でついてきます。マウスで動かして左クリックで確定"""
    bl_idname = OP_IDNAME
    bl_label = T.TOOL_NAME
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING', 'DEPENDS_ON_CURSOR'}
    bl_cursor_pending = 'CROSSHAIR'

    mode: EnumProperty(name="動かし方", items=MODE_ITEMS, default='MOVE', options={'SKIP_SAVE'},
                       description="移動（G）・拡大縮小（S）・口を開く（A）")
    offset: FloatVectorProperty(name="移動量", subtype='TRANSLATION', size=3, options={'SKIP_SAVE'},
                                description="ワールド座標での移動量（口を開くときは下唇の動き）")
    scale: FloatProperty(name="倍率", default=1.0, soft_min=0.0, soft_max=4.0, options={'SKIP_SAVE'},
                         description="1 より大きいと広がり、小さいと縮む")
    center: FloatVectorProperty(name="拡大の中心", subtype='XYZ', size=3, options={'SKIP_SAVE'},
                                description="拡大縮小の中心（ワールド座標）。ふつうは選択の中心のまま")
    use_center: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})
    axis: EnumProperty(name="軸", items=AXIS_ITEMS, default='NONE', options={'SKIP_SAVE'},
                       description="この軸の向きだけに動かす（操作中は X・Y・Z キー）")
    axis_space: EnumProperty(name="軸の向き", items=SPACE_ITEMS, default='GLOBAL', options={'SKIP_SAVE'},
                             description="軸をワールドで取るか、オブジェクトの向きで取るか")
    axis_plane: BoolProperty(name="その軸を除く", default=False, options={'SKIP_SAVE'},
                             description="Shift＋軸キーのとき。その軸以外の 2 方向に動かす")
    radius: FloatProperty(name="範囲の半径", default=0.02, min=0.0, soft_max=1.0, subtype='DISTANCE',
                          unit='LENGTH', options={'SKIP_SAVE'}, description=D_RADIUS)
    falloff: EnumProperty(name="減衰", items=FALLOFF_ENUM, default='SMOOTH', options={'SKIP_SAVE'},
                          description="範囲のふちに向かって、動きがどう減るか（操作中は Shift+O）")
    connected: BoolProperty(name="つながった面だけ", default=True, options={'SKIP_SAVE'}, description=D_CONNECTED)
    snap: BoolProperty(name="面に沿わせる", default=True, options={'SKIP_SAVE'}, description=D_SNAP)
    reference: EnumProperty(name="沿わせる面", items=REF_ITEMS, default='START', options={'SKIP_SAVE'},
                            description="どの形の面に沿ってすべらせるか（操作中は B）")
    use_mirror: BoolProperty(name="左右対称（X）", default=False, options={'SKIP_SAVE'}, description=D_MIRROR)
    upper_ratio: FloatProperty(name="上唇の割合", default=0.25, min=0.0, max=1.0, options={'SKIP_SAVE'},
                               description=D_UPPER)
    open_round: BoolProperty(name="まるく開く", default=True, options={'SKIP_SAVE'}, description=D_ROUND)
    view_dir: FloatVectorProperty(size=3, options={'HIDDEN', 'SKIP_SAVE'})
    view_up: FloatVectorProperty(size=3, options={'HIDDEN', 'SKIP_SAVE'})
    # 「新しいキーを作って始める」から来たとき：やめたら作ったキーを消す（空のキーを残さない）
    made_key: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    made_basis: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and context.mode == 'EDIT_MESH'

    # ---- 共通 ----

    def _make_ctx(self, context):
        obj = context.edit_object
        bm = bmesh.from_edit_mesh(obj.data)
        layer = None
        keys = obj.data.shape_keys
        if keys is not None and obj.active_shape_key_index != 0:
            layer = bm.verts.layers.shape.get(keys.reference_key.name)
        vd = Vector(self.view_dir)
        vu = Vector(self.view_up)
        ctx = core.SlideContext(bm, obj.matrix_world, basis_layer=layer,
                                use_basis=(self.reference == 'BASIS'),
                                view_dir=vd if vd.length > 1e-9 else None,
                                view_up=vu if vu.length > 1e-9 else None)
        return obj, bm, ctx

    def _weights(self, ctx):
        ctx.compute_weights(self.radius, self.falloff, self.connected, mirror=self.use_mirror,
                            mode=self.mode, open_round=self.open_round)

    def _solve(self, obj, bm, ctx, touched):
        center = Vector(self.center) if self.use_center else ctx.center_world
        pos = ctx.solve(mode=self.mode, offset=Vector(self.offset), scale=self.scale,
                        axis_vec=_axis_vec(self, obj), plane=self.axis_plane, snap=self.snap,
                        mirror=self.use_mirror, upper_ratio=self.upper_ratio, center=center)
        core.apply_positions(bm, ctx, pos, touched)
        bmesh.update_edit_mesh(obj.data, loop_triangles=True, destructive=False)
        return set(pos.keys())

    def execute(self, context):
        obj = context.edit_object
        bad = _check_target(obj)
        if bad:
            self.report({bad[0]}, bad[1])
            return {'CANCELLED'}
        obj, bm, ctx = self._make_ctx(context)
        if not ctx.selected:
            self.report({'WARNING'}, T.MSG['no_selection'])
            return {'CANCELLED'}
        self._weights(ctx)
        if self.mode == 'OPEN' and not (ctx.open_info and (ctx.open_info['upper'] + ctx.open_info['lower'])):
            self.report({'WARNING'}, T.MSG['open_failed'])
            return {'CANCELLED'}
        self._solve(obj, bm, ctx, ())
        return {'FINISHED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        obj = context.edit_object
        if obj is not None:
            layout.label(text=f"書き込み先：{T.target_label(obj)}", icon='SHAPEKEY_DATA')
        col = layout.column()
        # 「動かし方」の見出しを付けると幅が足りず「拡大…」と切れるので、横いっぱいに並べる
        row = col.row()
        row.use_property_split = False
        row.prop(self, "mode", expand=True)
        if self.mode == 'SCALE':
            col.prop(self, "scale")
            col.prop(self, "center")
        else:
            col.prop(self, "offset")
        if self.mode == 'OPEN':
            col.prop(self, "upper_ratio")
            col.prop(self, "open_round")
        col.row().prop(self, "axis", expand=True)
        if self.axis != 'NONE':
            col.row().prop(self, "axis_space", expand=True)
            col.prop(self, "axis_plane")
        col.separator()
        col.label(text="範囲")
        col.prop(self, "radius")
        col.prop(self, "falloff")
        col.prop(self, "connected")
        col.separator()
        col.label(text="面")
        col.prop(self, "snap")
        if obj is not None and _has_keys(obj):
            col.row().prop(self, "reference", expand=True)
            col.label(text=f"ベースの形＝「{obj.data.shape_keys.reference_key.name}」")
        col.prop(self, "use_mirror")

    # ---- モーダル ----

    def invoke(self, context, event):
        global _live
        area, region, rv3d = _window_region(context)
        if region is None or rv3d is None:
            self.report({'WARNING'}, T.MSG['not_view3d'])
            return {'CANCELLED'}
        obj = context.edit_object
        bad = _check_target(obj)
        if bad:
            self.report({bad[0]}, bad[1])
            return {'CANCELLED'}
        if len(context.objects_in_mode) > 1:
            self.report({'WARNING'}, T.MSG['multi_object'].format(name=obj.name))

        st = _settings(context)
        ts = context.scene.tool_settings
        self.mode = 'MOVE'
        self.offset = (0.0, 0.0, 0.0)
        self.scale = 1.0
        self.axis = 'NONE'
        self.axis_space = 'GLOBAL'
        self.axis_plane = False
        self.connected = st.connected
        self.snap = st.snap
        self.reference = st.reference if _has_keys(obj) else 'START'
        self.upper_ratio = st.upper_ratio
        self.open_round = st.open_round
        self.use_mirror = obj.data.use_mirror_x
        if ts.proportional_edit_falloff in core.FALLOFF_IDS:
            self.falloff = ts.proportional_edit_falloff
        else:
            self.falloff = 'SMOOTH'
            self.report({'INFO'}, T.MSG['random_falloff'])
        self.view_dir = rv3d.view_rotation @ Vector((0.0, 0.0, -1.0))
        self.view_up = rv3d.view_rotation @ Vector((0.0, 1.0, 0.0))

        self.obj, self.bm, self.ctx = self._make_ctx(context)
        if not self.ctx.selected:
            self.report({'WARNING'}, T.MSG['no_selection'])
            self._drop_made_key(context)
            return {'CANCELLED'}
        radius, why = self.ctx.suggest_radius(ts.proportional_size, st.last_radius)
        self.radius = radius
        if why:
            key = 'radius_big' if why == 'BIG' else 'radius_small'
            self.report({'INFO'}, T.MSG[key].format(old=T.fmt_len(context.scene, ts.proportional_size),
                                                    new=T.fmt_len(context.scene, radius)))
        self.center = self.ctx.center_world
        self.use_center = True

        self.area, self.region, self.rv3d = area, region, rv3d
        self.scene = context.scene
        self.has_keys = _has_keys(obj)
        m = Vector((event.mouse_x - region.x, event.mouse_y - region.y))
        self.start_mouse = m.copy()
        self.last_mouse = m.copy()
        self.virtual = m.copy()
        self.offset_base = Vector((0.0, 0.0, 0.0))
        self.scale_base = 1.0
        self.scale_ref = 1.0
        self.scale_d0 = 0.0
        self.scale_near = False
        self.affected = 0
        self.num_text = ""
        self.num_adv = False
        self.num_error = False
        self.hint = ""
        self.nav_pending = False
        self.touched = set()
        self.offset_length = 0.0
        self.open_info = None
        self.mirror_missing = 0
        self.visible = self.ctx.n - sum(self.ctx.hidden)
        self._rebuild_weights()

        _live = self
        context.window_manager.modal_handler_add(self)
        self._update(context)
        return {'RUNNING_MODAL'}

    # 量の計算

    def _center_2d(self):
        c2 = view3d_utils.location_3d_to_region_2d(self.region, self.rv3d, Vector(self.center))
        if c2 is None:
            c2 = Vector((self.region.width / 2, self.region.height / 2))
        return c2

    def _point_on_constraint(self, m2, avec):
        r, rv = self.region, self.rv3d
        c = Vector(self.center)
        o = view3d_utils.region_2d_to_origin_3d(r, rv, m2)
        d = view3d_utils.region_2d_to_vector_3d(r, rv, m2)
        if self.axis_plane:
            if abs(d.normalized().dot(avec)) < 0.02:
                return None
            return geometry.intersect_line_plane(o, o + d, c, avec)
        if abs(d.normalized().dot(avec)) > 0.98:
            return None
        res = geometry.intersect_line_line(o, o + d, c, c + avec)
        return res[1] if res else None

    def _mouse_delta(self):
        r, rv = self.region, self.rv3d
        c = Vector(self.center)
        avec = _axis_vec(self, self.obj)
        if avec is not None:
            a = self._point_on_constraint(self.start_mouse, avec)
            b = self._point_on_constraint(self.virtual, avec)
            if a is not None and b is not None:
                return b - a
            if not self.axis_plane:
                # 軸が画面の奥を向いている：画面の縦の動きを軸の向きに使う
                p0 = view3d_utils.region_2d_to_location_3d(r, rv, self.start_mouse, c)
                p1 = view3d_utils.region_2d_to_location_3d(r, rv, self.start_mouse + Vector((0.0, 1.0)), c)
                return avec * ((self.virtual.y - self.start_mouse.y) * (p1 - p0).length)
        a = view3d_utils.region_2d_to_location_3d(r, rv, self.start_mouse, c)
        b = view3d_utils.region_2d_to_location_3d(r, rv, self.virtual, c)
        d = core.project_offset(b - a, avec, self.axis_plane)
        if self.mode == 'OPEN' and avec is None:
            up = self.ctx.view_up_world
            d = up * d.dot(up)
        return d

    def _numeric_value(self):
        text = self.num_text
        us = self.scene.unit_settings
        try:
            if self.mode == 'SCALE':
                return float(bpy.utils.units.to_value('NONE', 'NONE', text))
            if us.system == 'NONE':
                return float(bpy.utils.units.to_value('NONE', 'NONE', text))
            return bpy.utils.units.to_value(us.system, 'LENGTH', text) / (us.scale_length or 1.0)
        except Exception:  # noqa: BLE001
            return None

    def _numeric_offset(self, val):
        avec = _axis_vec(self, self.obj)
        if avec is not None and not self.axis_plane:
            return avec * val
        if self.mode == 'OPEN':
            return self.ctx.view_up_world * (-val)
        k = 1 if (self.axis_plane and self.axis == 'X') else 0
        v = Vector((0.0, 0.0, 0.0))
        v[k] = val
        return v

    def _update(self, context):
        self.hint = self._hint_text()
        if self.num_text:
            val = self._numeric_value()
            self.num_error = val is None
            if val is not None:
                if self.mode == 'SCALE':
                    self.scale = val
                    self.offset = (0.0, 0.0, 0.0)
                else:
                    self.offset = self._numeric_offset(val)
                    self.scale = 1.0
        elif self.mode == 'SCALE':
            # 中心からの距離の増えた分で倍率を決める。始めの距離が十分あれば Blender の S と同じ「距離の比」になる
            d = (self.virtual - self._center_2d()).length
            self.scale = self.scale_base * (1.0 + (d - self.scale_d0) / max(self.scale_ref, 1.0))
            self.offset = (0.0, 0.0, 0.0)
        else:
            self.offset = self.offset_base + self._mouse_delta()
            self.scale = 1.0
        self.offset_length = Vector(self.offset).length
        self.touched = self._solve(self.obj, self.bm, self.ctx, self.touched)
        self._refresh_ui(context)

    def _hint_text(self):
        if self.mode == 'OPEN':
            ctx = self.ctx
            xs = [ctx.start_world[i].dot(Vector(self.view_right_world())) for i in ctx.seeds] if ctx.seeds else []
            width = (max(xs) - min(xs)) if xs else 0.0
            if width > 0 and self.radius < 0.8 * width:
                return f"口を開くときは、範囲の半径を口の幅（{T.fmt_len(self.scene, width)}）くらいにすると形がきれいです"
        if self.mode == 'SCALE' and getattr(self, "scale_near", False):
            return "中心の近くから始めたので、離した距離で広がります（近づけると縮む）"
        return ""

    def view_right_world(self):
        return Vector(self.view_dir).cross(Vector(self.view_up)).normalized()

    def _refresh_ui(self, context):
        try:
            self.area.header_text_set(T.header_text(self, self.scene, self.obj))
            context.workspace.status_text_set(_status_draw)
            self.area.tag_redraw()
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def _rebuild_weights(self):
        self._weights(self.ctx)
        self.affected = len(self.ctx.weights)
        self.open_info = self.ctx.open_info
        self.mirror_missing = self.ctx.mirror_missing() if self.use_mirror else 0

    def _enter_mode(self, mode):
        prev = self.mode
        self.mode = mode
        self.num_text = ""
        self.num_adv = False
        self.offset_base = Vector((0.0, 0.0, 0.0))
        self.scale_base = 1.0
        self.offset = (0.0, 0.0, 0.0)
        self.scale = 1.0
        self.start_mouse = self.virtual.copy()
        if mode == 'SCALE':
            self._scale_reference()
        if prev == 'OPEN' or mode == 'OPEN':
            self._rebuild_weights()
        if mode == 'OPEN':
            oi = self.ctx.open_info
            if not oi or (oi['upper'] + oi['lower']) == 0:
                self.report({'WARNING'}, T.MSG['open_failed'])
                self.mode = prev
                self._rebuild_weights()

    def _scale_reference(self):
        """拡大縮小の基準。中心に近すぎる所から始めても、離した距離で素直に広がるようにする。"""
        d0 = (self.virtual - self._center_2d()).length
        m = 80.0 * bpy.context.preferences.view.ui_scale
        self.scale_d0 = d0
        self.scale_ref = max(d0, m)
        self.scale_near = d0 < m

    def _bake_for_navigation(self):
        if self.num_text:
            return
        if self.mode == 'SCALE':
            self.scale_base = self.scale
        else:
            self.offset_base = Vector(self.offset)
        self.nav_pending = True

    def _rebuild_ctx(self):
        for i in self.touched:
            self.bm.verts[i].co = self.ctx.start[i]
        self.touched = set()
        center = Vector(self.center)
        self.obj, self.bm, self.ctx = self._make_ctx(bpy.context)
        self.center = center
        self._rebuild_weights()

    def _finish(self, context):
        global _live
        _live = None
        try:
            self.area.header_text_set(None)
        except Exception:  # noqa: BLE001
            pass
        try:
            context.workspace.status_text_set(None)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.area.tag_redraw()
        except Exception:  # noqa: BLE001
            pass

    def _cancel(self, context):
        try:
            for i in self.touched:
                self.bm.verts[i].co = self.ctx.start[i]
            bmesh.update_edit_mesh(self.obj.data, loop_triangles=True, destructive=False)
        except (ReferenceError, AttributeError, IndexError):
            pass
        self._finish(context)
        self._drop_made_key(context)
        return {'CANCELLED'}

    def _drop_made_key(self, context):
        if not self.made_key:
            return
        obj = context.edit_object
        keys = obj.data.shape_keys if obj is not None else None
        kb = keys.key_blocks.get(self.made_key) if keys is not None else None
        if kb is None:
            return
        bpy.ops.object.mode_set(mode='OBJECT')
        if self.made_basis and len(keys.key_blocks) == 2:
            obj.shape_key_clear()
        else:
            obj.shape_key_remove(kb)
        bpy.ops.object.mode_set(mode='EDIT')
        self.made_key = ""
        self.report({'INFO'}, T.MSG['key_dropped'])

    def cancel(self, context):
        self._cancel(context)

    def modal(self, context, event):
        try:
            return self._modal(context, event)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self.report({'ERROR'}, T.MSG['error'])
            return self._cancel(context)

    def _modal(self, context, event):
        t, v = event.type, event.value

        if t == 'MOUSEMOVE':
            cur = Vector((event.mouse_x - self.region.x, event.mouse_y - self.region.y))
            if self.nav_pending:
                self.nav_pending = False
                self.last_mouse = cur.copy()
                self.virtual = cur.copy()
                self.start_mouse = cur.copy()
                if self.mode == 'SCALE':
                    self._scale_reference()
                return {'RUNNING_MODAL'}
            k = 0.1 if event.shift else 1.0
            self.virtual += (cur - self.last_mouse) * k
            self.last_mouse = cur
            self._update(context)
            return {'RUNNING_MODAL'}

        # Alt を押しながらの視点操作は Blender に渡す
        if event.alt and t in {'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'TRACKPADPAN',
                               'TRACKPADZOOM', 'MOUSEROTATE', 'MOUSESMARTZOOM'}:
            self._bake_for_navigation()
            return {'PASS_THROUGH'}

        if v != 'PRESS':
            return {'RUNNING_MODAL'}

        if t in {'LEFTMOUSE', 'RET', 'NUMPAD_ENTER'} or (t == 'SPACE' and not self.num_adv):
            return self._confirm(context)
        if t in {'RIGHTMOUSE', 'ESC'}:
            return self._cancel(context)

        if t in {'WHEELDOWNMOUSE', 'PAGE_UP', 'WHEELUPMOUSE', 'PAGE_DOWN'}:
            f = 1.01 if event.shift else 1.1
            self.radius = self.radius * f if t in {'WHEELDOWNMOUSE', 'PAGE_UP'} else self.radius / f
            self._rebuild_weights()
            self._update(context)
            return {'RUNNING_MODAL'}

        # 数値入力
        if t == 'BACK_SPACE':
            if self.num_text:
                self.num_text = "" if event.ctrl else self.num_text[:-1]
                if not self.num_text:
                    self.num_adv = False
                self._update(context)
            return {'RUNNING_MODAL'}
        uni = event.unicode
        if uni and not event.ctrl and not event.oskey:
            if self.num_adv and uni.isprintable():
                self.num_text += uni
                self._update(context)
                return {'RUNNING_MODAL'}
            if uni == '=' and not self.num_adv:
                self.num_adv = True
                self.num_text = self.num_text or ""
                self._update(context)
                return {'RUNNING_MODAL'}
            if uni in "0123456789.-" and len(uni) == 1:
                self.num_text += uni
                self._update(context)
                return {'RUNNING_MODAL'}

        if event.ctrl or event.oskey or event.is_repeat:
            return {'RUNNING_MODAL'}

        if t in {'G', 'S', 'A'}:
            self._enter_mode({'G': 'MOVE', 'S': 'SCALE', 'A': 'OPEN'}[t])
            self._update(context)
        elif t in {'X', 'Y', 'Z'}:
            plane = event.shift
            if self.axis != t or self.axis_plane != plane:
                self.axis, self.axis_plane, self.axis_space = t, plane, 'GLOBAL'
            elif self.axis_space == 'GLOBAL':
                self.axis_space = 'LOCAL'
            else:
                self.axis, self.axis_plane = 'NONE', False
            self._update(context)
        elif t == 'C':
            self.axis, self.axis_plane = 'NONE', False
            self._update(context)
        elif t == 'O' and event.alt:
            self.connected = not self.connected
            self._rebuild_weights()
            self._update(context)
        elif t == 'O':
            i = core.FALLOFF_IDS.index(self.falloff)
            self.falloff = core.FALLOFF_IDS[(i + 1) % len(core.FALLOFF_IDS)]
            self._rebuild_weights()
            self._update(context)
        elif t == 'F':
            self.snap = not self.snap
            self._update(context)
        elif t == 'B' and self.has_keys:
            self.reference = 'START' if self.reference == 'BASIS' else 'BASIS'
            self._rebuild_ctx()
            self._update(context)
        elif t == 'M':
            self.use_mirror = not self.use_mirror
            self._rebuild_weights()
            self._update(context)
        return {'RUNNING_MODAL'}

    def _confirm(self, context):
        if self.num_text and self.num_error:
            return {'RUNNING_MODAL'}
        ts = context.scene.tool_settings
        ts.proportional_size = self.radius
        ts.proportional_edit_falloff = self.falloff
        _settings(context).last_radius = self.radius
        self._finish(context)
        return {'FINISHED'}


# ---------------------------------------------------------------- 新しいキーを作って始める

class MESH_OT_surface_slide_new_key(bpy.types.Operator):
    """新しいシェイプキーを作って書き込み先にし、そのまま「面に沿ってスライド」を始めます"""
    bl_idname = "mesh.surface_slide_new_key"
    bl_label = T.NEW_KEY_TEXT
    bl_options = {'REGISTER', 'UNDO', 'DEPENDS_ON_CURSOR'}
    bl_cursor_pending = 'CROSSHAIR'

    name: StringProperty(name="キーの名前", default="新しい表情")

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and obj.type == 'MESH' and context.mode == 'EDIT_MESH'

    def _make_key(self, context):
        obj = context.edit_object
        bpy.ops.object.mode_set(mode='OBJECT')
        self.made_basis = obj.data.shape_keys is None
        if self.made_basis:
            obj.shape_key_add(name="Basis", from_mix=False)
        kb = obj.shape_key_add(name=self.name, from_mix=False)
        kb.value = 1.0
        obj.active_shape_key_index = list(obj.data.shape_keys.key_blocks).index(kb)
        bpy.ops.object.mode_set(mode='EDIT')
        return kb

    def execute(self, context):
        kb = self._make_key(context)
        self.report({'INFO'}, f"キー「{kb.name}」を作りました")
        return {'FINISHED'}

    def invoke(self, context, event):
        area, region, rv3d = _window_region(context)
        if region is None:
            self.report({'WARNING'}, T.MSG['not_view3d'])
            return {'CANCELLED'}
        try:
            nsel = context.edit_object.data.count_selected_items()[0]
        except Exception:  # noqa: BLE001
            nsel = 1
        if not nsel:
            self.report({'WARNING'}, T.MSG['no_selection'])
            return {'CANCELLED'}
        kb = self._make_key(context)
        with context.temp_override(area=area, region=region):
            bpy.ops.mesh.surface_slide_proportional('INVOKE_DEFAULT', made_key=kb.name, made_basis=self.made_basis)
        return {'FINISHED'}


class MESH_OT_surface_slide_help(bpy.types.Operator):
    """面に沿ってスライドの使い方（マニュアル）をブラウザで開きます"""
    bl_idname = "mesh.surface_slide_help"
    bl_label = "使い方を開く"

    def execute(self, context):
        if not os.path.exists(MANUAL_PATH):
            self.report({'ERROR'}, f"マニュアルが見つかりません：{MANUAL_PATH}")
            return {'CANCELLED'}
        bpy.ops.wm.path_open(filepath=MANUAL_PATH)
        return {'FINISHED'}


# ---------------------------------------------------------------- N パネル

class VIEW3D_PT_surface_slide(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = T.PANEL_TAB
    bl_label = T.TOOL_NAME

    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'

    def draw_header_preset(self, context):
        self.layout.operator(MESH_OT_surface_slide_help.bl_idname, text="", icon='HELP', emboss=False)

    def draw(self, context):
        layout = self.layout
        obj = context.edit_object
        st = _settings(context)
        ts = context.scene.tool_settings

        row = layout.row()
        row.scale_y = 1.4
        row.operator(OP_IDNAME, text="スライドを始める", icon='MOD_SHRINKWRAP')
        layout.label(text=f"ショートカット：{_kmi_text()}")

        box = layout.box()
        col = box.column(align=True)
        nsel = 0
        if obj is not None:
            try:
                nsel = obj.data.count_selected_items()[0]
            except Exception:  # noqa: BLE001
                nsel = obj.data.total_vert_sel
        if nsel:
            col.label(text=f"選択中：{nsel} 頂点", icon='VERTEXSEL')
        else:
            col.label(text="動かしたい頂点を選んでください", icon='INFO')
        if obj is not None:
            col.label(text=f"書き込み先：{T.target_label(obj)}", icon='SHAPEKEY_DATA')
            bad = _check_target(obj)
            if bad:
                kb = obj.active_shape_key
                if kb is not None and kb.lock_shape:
                    col.label(text=f"「{kb.name}」はロックされています", icon='LOCKED')
                else:
                    col.label(text="書き込み先がベース（Basis）です", icon='ERROR')
                    col.label(text="シェイプキーの一覧でキーを選んでください")
        layout.operator(MESH_OT_surface_slide_new_key.bl_idname, text="新しいキーを作って始める", icon='ADD')

        layout.label(text="はじめの設定")
        col = layout.column()
        sp = col.column()
        sp.use_property_split = True
        sp.use_property_decorate = False
        sp.prop(st, "radius")
        sp.prop(ts, "proportional_edit_falloff", text="減衰")
        col.prop(st, "connected")
        col.prop(st, "snap")
        if obj is not None and _has_keys(obj):
            col.label(text="沿わせる面")
            col.row().prop(st, "reference", expand=True)
        if obj is not None:
            col.prop(obj.data, "use_mirror_x", text="左右対称（X）")
        col.separator()
        col.label(text="口を開く（操作中に A）")
        sp = col.column()
        sp.use_property_split = True
        sp.use_property_decorate = False
        sp.prop(st, "upper_ratio")
        col.prop(st, "open_round")
        layout.separator()
        layout.operator(MESH_OT_surface_slide_help.bl_idname, icon='HELP')


# ---------------------------------------------------------------- 描画

def _status_draw(header, context):
    from bl_ui.space_statusbar import STATUSBAR_HT_header
    op = _live
    if op is None:
        orig = getattr(STATUSBAR_HT_header, "_draw_orig", None)
        if orig is not None and orig is not _status_draw:
            orig(header, context)
        return
    try:
        items = T.status_items(op, op.has_keys)
    except (ReferenceError, AttributeError):
        return
    layout = header.layout
    for icons, text, active, gap in items:
        row = layout.row(align=True)
        row.active = active
        for ic in icons[:-1]:
            row.label(text="", icon=ic)
        if icons:
            row.label(text=text, icon=icons[-1])
        else:
            row.label(text=text)
        layout.separator(factor=1.4 if gap else 0.4)


def _selection_centroid(op):
    mw = op.obj.matrix_world
    ids = op.ctx.selected
    c = Vector((0.0, 0.0, 0.0))
    for i in ids:
        c += mw @ op.bm.verts[i].co
    return c / len(ids) if ids else Vector(op.center)


def _draw_view():
    global _live
    op = _live
    if op is None:
        return
    try:
        if bpy.context.region is None or bpy.context.region.as_pointer() != op.region.as_pointer():
            return
        mw = op.obj.matrix_world
        bm = op.bm
        coords, colors = [], []
        for i, w in op.ctx.weights.items():
            coords.append(mw @ bm.verts[i].co)
            colors.append((0.2 + 0.8 * w, 0.4 + 0.15 * w, 1.0 - 0.9 * w, 1.0))
        space = bpy.context.space_data
        xray = bool(space and space.shading.show_xray)
        if coords:
            shader = gpu.shader.from_builtin('POINT_FLAT_COLOR')
            gpu.state.point_size_set(5.0 * bpy.context.preferences.view.ui_scale)
            gpu.state.blend_set('ALPHA')
            if xray:
                gpu.state.depth_test_set('NONE')
                batch_for_shader(shader, 'POINTS', {"pos": coords, "color": colors}).draw(shader)
            else:
                gpu.state.depth_test_set('LESS_EQUAL')
                batch_for_shader(shader, 'POINTS', {"pos": coords, "color": colors}).draw(shader)
                gpu.state.depth_test_set('GREATER')
                faint = [(r, g, b, 0.15) for r, g, b, _a in colors]
                batch_for_shader(shader, 'POINTS', {"pos": coords, "color": faint}).draw(shader)
        # 軸の線
        avec = _axis_vec(op, op.obj)
        if avec is not None:
            theme = bpy.context.preferences.themes[0].user_interface
            col = {'X': theme.axis_x, 'Y': theme.axis_y, 'Z': theme.axis_z}[op.axis]
            c = Vector(op.center)
            L = max(op.rv3d.view_distance * 10.0, op.radius * 50.0)
            lines = []
            if op.axis_plane:
                # 平面：残りの 2 方向に線を引く
                for k in range(3):
                    if 'XYZ'[k] == op.axis:
                        continue
                    other = Vector((0.0, 0.0, 0.0))
                    other[k] = 1.0
                    if op.axis_space == 'LOCAL':
                        other = (op.obj.matrix_world.to_3x3() @ other).normalized()
                    lines += [c - other * L, c + other * L]
            else:
                lines = [c - avec * L, c + avec * L]
            sh = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
            gpu.state.depth_test_set('NONE')
            sh.bind()
            sh.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
            sh.uniform_float("lineWidth", 1.5)
            sh.uniform_float("color", (col[0], col[1], col[2], 0.9))
            batch_for_shader(sh, 'LINES', {"pos": lines}).draw(sh)
    except ReferenceError:
        _live = None
    finally:
        gpu.state.depth_test_set('NONE')
        gpu.state.blend_set('NONE')
        gpu.state.point_size_set(1.0)


def _draw_pixel():
    global _live
    op = _live
    if op is None:
        return
    try:
        region, rv3d = bpy.context.region, bpy.context.region_data
        if region is None or region.as_pointer() != op.region.as_pointer():
            return
        ui = bpy.context.preferences.view.ui_scale
        c = _selection_centroid(op)
        c2 = view3d_utils.location_3d_to_region_2d(region, rv3d, c)
        right = rv3d.view_rotation @ Vector((1.0, 0.0, 0.0))
        e2 = view3d_utils.location_3d_to_region_2d(region, rv3d, c + right * op.radius)
        if c2 is None or e2 is None:
            return
        r = (e2 - c2).length
        gpu.state.blend_set('ALPHA')
        sh = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
        sh.bind()
        sh.uniform_float("viewportSize", gpu.state.viewport_get()[2:])
        sh.uniform_float("lineWidth", 1.5)
        sh.uniform_float("color", (1.0, 1.0, 1.0, 0.8))
        pts = [(c2.x + r * math.cos(a * math.tau / 96), c2.y + r * math.sin(a * math.tau / 96), 0.0) for a in range(97)]
        batch_for_shader(sh, 'LINE_STRIP', {"pos": pts}).draw(sh)
        # 拡大縮小：中心からマウスまでの点線
        if op.mode == 'SCALE' and not op.num_text:
            cc = op._center_2d()
            vv = op.virtual
            seg = []
            n = max(2, int((vv - cc).length / (6 * ui)))
            for k in range(0, n - 1, 2):
                seg += [tuple(cc.lerp(vv, k / n)) + (0.0,), tuple(cc.lerp(vv, (k + 1) / n)) + (0.0,)]
            if seg:
                sh.uniform_float("color", (1.0, 1.0, 1.0, 0.6))
                batch_for_shader(sh, 'LINES', {"pos": seg}).draw(sh)
        # 文字
        fid = 0
        blf.size(fid, 12 * ui)
        blf.enable(fid, blf.SHADOW)
        blf.shadow(fid, 3, 0.0, 0.0, 0.0, 0.8)
        blf.shadow_offset(fid, 1, -1)
        blf.color(fid, 1.0, 1.0, 1.0, 0.95)
        label = f"範囲 {T.fmt_len(op.scene, op.radius)}"
        tx = c2.x + r * 0.7071 + 6 * ui
        ty = c2.y + r * 0.7071 + 6 * ui
        if r > max(region.width, region.height):
            tx, ty = op.last_mouse.x + 18 * ui, op.last_mouse.y - 24 * ui
            label = f"範囲 {T.fmt_len(op.scene, op.radius)}：画面より大きいです（ホイール上で小さく）"
        tx = min(max(tx, 8 * ui), region.width - blf.dimensions(fid, label)[0] - 8 * ui)
        ty = min(max(ty, 8 * ui), region.height - 20 * ui)
        blf.position(fid, tx, ty, 0)
        blf.draw(fid, label)
        blf.disable(fid, blf.SHADOW)
    except ReferenceError:
        _live = None
    finally:
        gpu.state.blend_set('NONE')


# ---------------------------------------------------------------- メニュー・登録

def _menu(self, context):
    lay = self.layout
    lay.separator()
    old = lay.operator_context
    lay.operator_context = 'INVOKE_REGION_WIN'
    lay.operator(OP_IDNAME, text=T.MENU_TEXT, icon='MOD_SHRINKWRAP')
    lay.operator(MESH_OT_surface_slide_new_key.bl_idname, text=T.NEW_KEY_TEXT, icon='ADD')
    lay.operator_context = old


CLASSES = (
    SurfaceSlideSettings,
    MESH_OT_surface_slide_proportional,
    MESH_OT_surface_slide_new_key,
    MESH_OT_surface_slide_help,
    VIEW3D_PT_surface_slide,
)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.surface_slide = PointerProperty(type=SurfaceSlideSettings)
    bpy.types.VIEW3D_MT_edit_mesh_vertices.append(_menu)
    bpy.types.VIEW3D_MT_edit_mesh_context_menu.append(_menu)
    _draw_handles.append(bpy.types.SpaceView3D.draw_handler_add(_draw_view, (), 'WINDOW', 'POST_VIEW'))
    _draw_handles.append(bpy.types.SpaceView3D.draw_handler_add(_draw_pixel, (), 'WINDOW', 'POST_PIXEL'))
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name="Mesh", space_type='EMPTY')
        kmi = km.keymap_items.new(OP_IDNAME, 'V', 'PRESS', shift=True, alt=True)
        _keymaps.append((km, kmi))


def unregister():
    global _live
    _live = None
    try:
        from bl_ui.space_statusbar import STATUSBAR_HT_header
        orig = getattr(STATUSBAR_HT_header, "_draw_orig", None)
        if orig is not None and STATUSBAR_HT_header.draw is _status_draw:
            STATUSBAR_HT_header.draw = orig
    except Exception:  # noqa: BLE001
        pass
    for km, kmi in _keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:  # noqa: BLE001
            pass
    _keymaps.clear()
    for h in _draw_handles:
        bpy.types.SpaceView3D.draw_handler_remove(h, 'WINDOW')
    _draw_handles.clear()
    bpy.types.VIEW3D_MT_edit_mesh_context_menu.remove(_menu)
    bpy.types.VIEW3D_MT_edit_mesh_vertices.remove(_menu)
    del bpy.types.Scene.surface_slide
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
