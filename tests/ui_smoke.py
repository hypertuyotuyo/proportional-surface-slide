"""画面つきの Blender で、キー操作を疑似イベントで通して確かめ、撮る。

ふだん使っている Blender・設定には触らない（別の設定フォルダ＋--factory-startup、アドオンは sys.path から登録）。

    set BLENDER_USER_RESOURCES=<空の一時フォルダ>
    blender --factory-startup --enable-event-simulate --no-window-focus --window-geometry 60 60 1600 950
            --python tests/ui_smoke.py -- <出力フォルダ>

疑似イベントには必ずマウスの x, y を付ける（付けないと Blender 本体のショートカットも動かない）。
"""
import os
import sys
import traceback

import bpy
import bmesh
from mathutils import Vector

ADDON_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ADDON_PARENT)
import proportional_surface_slide as pss  # noqa: E402

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
OUT = argv[0] if argv else os.path.join(os.path.expanduser("~"), "pss_ui")
os.makedirs(OUT, exist_ok=True)
LOG = os.path.join(OUT, "log.txt")
open(LOG, "w", encoding="utf-8").close()
S = {"fail": 0, "mouse": (0, 0)}


def log(*a):
    line = " ".join(str(x) for x in a)
    print("UISMOKE", line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def check(cond, msg):
    log(("OK   " if cond else "NG   ") + msg)
    if not cond:
        S["fail"] += 1


def win():
    return bpy.context.window_manager.windows[0]


def area_region():
    w = win()
    area = next(a for a in w.screen.areas if a.type == 'VIEW_3D')
    region = next(r for r in area.regions if r.type == 'WINDOW')
    return area, region


def shot(name):
    w = win()
    with bpy.context.temp_override(window=w, screen=w.screen):
        bpy.ops.screen.screenshot(filepath=os.path.join(OUT, name))
    log("saved", name)


def move(x, y):
    S["mouse"] = (int(x), int(y))
    win().event_simulate(type='MOUSEMOVE', value='NOTHING', x=int(x), y=int(y))


def send(t, value='PRESS', **mods):
    x, y = S["mouse"]
    win().event_simulate(type=t, value=value, x=x, y=y, **mods)


def tap(t, **mods):
    x, y = S["mouse"]
    unicode = mods.pop("unicode", None)
    kw = dict(x=x, y=y, **mods)
    if unicode is not None:
        win().event_simulate(type=t, value='PRESS', unicode=unicode, **kw)
    else:
        win().event_simulate(type=t, value='PRESS', **kw)
    win().event_simulate(type=t, value='RELEASE', **kw)


def shortcut():
    send('LEFT_SHIFT')
    send('LEFT_ALT', shift=True)
    tap('V', shift=True, alt=True)
    send('LEFT_ALT', 'RELEASE', shift=True)
    send('LEFT_SHIFT', 'RELEASE')


def live():
    return pss._live


def front_view():
    area, region = area_region()
    with bpy.context.temp_override(window=win(), area=area, region=region):
        bpy.ops.view3d.view_axis(type='FRONT')
        bpy.ops.view3d.view_selected()


def build_sphere():
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=0.1)
    obj = bpy.context.object
    obj.shape_key_add(name="Basis")
    obj.shape_key_add(name="mouth_open")
    obj.active_shape_key_index = 1
    front_view()
    bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(obj.data)
    for v in bm.verts:
        v.select = abs(v.co.z) < 0.003 and v.co.y < -0.097
    bm.select_flush_mode()
    bmesh.update_edit_mesh(obj.data)
    bpy.context.scene.tool_settings.proportional_size = 1.0  # factory の値（大きすぎ）
    return obj


STEPS = []


def step(delay=0.4):
    def deco(fn):
        STEPS.append((fn, delay))
        return fn
    return deco


@step(1.5)
def s_setup():
    w = win()
    w.event_simulate(type='ESC', value='PRESS', x=10, y=10)
    w.event_simulate(type='ESC', value='RELEASE', x=10, y=10)
    # 終了時の quit.blend をふだんの一時フォルダに書かない
    tmp = os.path.join(OUT, "_tmp")
    os.makedirs(tmp, exist_ok=True)
    bpy.context.preferences.filepaths.temporary_directory = tmp
    bpy.context.preferences.use_preferences_save = False
    pss.register()
    obj = build_sphere()
    log("verts", len(obj.data.vertices))


@step()
def s_shortcut():
    area, region = area_region()
    S["c"] = (region.x + region.width // 2, region.y + region.height // 2)
    move(*S["c"])
    shortcut()


@step()
def s_after_shortcut():
    op = live()
    check(op is not None, "Shift+Alt+V で始まった")
    if op is None:
        raise RuntimeError("not live")
    check(op.radius < 0.2, f"factory の半径 1 m は直された（{op.radius:.4f}）")
    S["r0"] = op.radius
    cx, cy = S["c"]
    for k in range(1, 6):
        move(cx, cy + k * 10)


@step()
def s_wheel():
    op = live()
    check(op.offset_length > 0, f"マウスで動いた（{op.offset_length:.4f}）")
    tap('WHEELDOWNMOUSE')


@step()
def s_wheel_check():
    op = live()
    check(abs(op.radius / S["r0"] - 1.1) < 1e-6, f"ホイール下で半径 ×1.1（{op.radius / S['r0']:.4f}）")
    S["r1"] = op.radius
    send('LEFT_SHIFT')
    tap('WHEELDOWNMOUSE', shift=True)
    send('LEFT_SHIFT', 'RELEASE')


@step()
def s_keys1():
    op = live()
    check(abs(op.radius / S["r1"] - 1.01) < 1e-6, f"Shift＋ホイール下で ×1.01（{op.radius / S['r1']:.4f}）")
    S["r2"] = op.radius
    send('LEFT_CTRL')
    tap('Z', ctrl=True)
    send('LEFT_CTRL', 'RELEASE')


@step()
def s_keys2():
    op = live()
    check(op.axis == 'NONE', f"Ctrl+Z では軸にならない（{op.axis}）")
    tap('Z')


@step()
def s_keys3():
    op = live()
    check(op.axis == 'Z' and op.axis_space == 'GLOBAL', f"Z でグローバル Z（{op.axis} {op.axis_space}）")
    shot("01_axis_z.png")
    tap('Z')


@step()
def s_keys4():
    op = live()
    check(op.axis == 'Z' and op.axis_space == 'LOCAL', f"もう一度 Z でローカル（{op.axis_space}）")
    tap('C')


@step()
def s_keys5():
    op = live()
    check(op.axis == 'NONE', "C で軸を解除")
    S["conn"] = op.connected
    send('LEFT_ALT')
    tap('O', alt=True)
    send('LEFT_ALT', 'RELEASE')


@step()
def s_keys6():
    op = live()
    check(op.connected != S["conn"], f"Alt+O でつながった面だけが切り替わる（{op.connected}）")
    S["fall"] = op.falloff
    send('LEFT_SHIFT')
    tap('O', shift=True)
    send('LEFT_SHIFT', 'RELEASE')


@step()
def s_keys7():
    op = live()
    check(op.falloff != S["fall"], f"Shift+O で減衰が進む（{S['fall']} → {op.falloff}）")
    tap('F')
    tap('M')


@step()
def s_keys8():
    op = live()
    check(op.snap is False and op.use_mirror is True, f"F で面に沿う OFF・M で左右対称 ON（{op.snap} {op.use_mirror}）")
    shot("02_dragging_status.png")
    move(*S["c"])  # 選択の中心の上
    tap('S')


@step()
def s_scale1():
    op = live()
    check(op.mode == 'SCALE' and abs(op.scale - 1.0) < 1e-9 and op.offset_length == 0, f"S にした瞬間は ×1（{op.scale}）")
    cx, cy = S["c"]
    move(cx + 30, cy)


@step()
def s_scale2():
    op = live()
    check(1.2 <= op.scale <= 1.5, f"中心の上から始めて右へ 30px → ×{op.scale:.3f}（1.2〜1.5）")
    shot("03_scale.png")
    tap('G')


@step()
def s_move_again():
    op = live()
    check(op.mode == 'MOVE' and op.offset_length == 0, "G に戻すと移動量 0 から")
    tap('A')


@step()
def s_open_fail():
    op = live()
    check(op.mode == 'MOVE', f"球では口を開けない → 移動のまま（{op.mode}）")
    tap('Z')
    for ch, t in [("0", 'ZERO'), (".", 'PERIOD'), ("0", 'ZERO'), ("1", 'ONE'), ("5", 'FIVE')]:
        tap(t, unicode=ch)


@step()
def s_numeric():
    op = live()
    check(op.num_text == "0.015", f"数値入力 {op.num_text!r}")
    shot("04_numeric.png")
    tap('RET')


@step()
def s_after_confirm():
    check(live() is None, "Enter で確定")
    last = bpy.context.window_manager.operators[-1]
    off = Vector(last.properties.offset)
    check(last.bl_idname == "MESH_OT_surface_slide_proportional" and (off - Vector((0, 0, 0.015))).length < 1e-9,
          f"数値どおり (0, 0, 0.015) 動いた（{tuple(round(x, 6) for x in off)}）")
    from bl_ui.space_statusbar import STATUSBAR_HT_header
    check(STATUSBAR_HT_header.draw is getattr(STATUSBAR_HT_header, "_draw_orig", None), "ステータスバーが元に戻った")
    ts = bpy.context.scene.tool_settings
    check(abs(ts.proportional_size - S["r2"]) < 1e-6, "確定した半径が Blender の設定に書き戻された")
    shot("05_confirmed.png")
    move(*S["c"])
    shortcut()


@step()
def s_second():
    op = live()
    check(op is not None and op.mode == 'MOVE' and op.snap is True and op.axis == 'NONE',
          f"2 回目は毎回同じ状態から（{op and (op.mode, op.snap, op.axis)}）")
    check(op is not None and abs(op.radius - S["r2"]) < 1e-6, "2 回目は前回の半径（自分で決めた値は直さない）")
    tap('ESC')


@step()
def s_rot_start():
    check(live() is None, "Esc でやめた（回転の前）")
    cx, cy = S["c"]
    move(cx + 80, cy)
    shortcut()


@step()
def s_rot_r():
    op = live()
    check(op is not None, "回転のために始めた")
    tap('R')


@step()
def s_rot_turn():
    op = live()
    check(op.mode == 'ROTATE' and op.angle == 0.0, f"R で回転になった（{op.mode}・{op.angle}）")
    c2 = op._rot_center_2d()
    S["rc"] = (c2.x + op.region.x, c2.y + op.region.y)
    rx, ry = S["rc"]
    import math as _m
    # R を押した所の角度から、中心のまわりに左回りで 90°
    a0 = op.rot_last
    rr = max(60.0, (op.virtual - c2).length)
    for k in range(1, 10):
        a = a0 + _m.radians(10 * k)
        move(rx + rr * _m.cos(a), ry + rr * _m.sin(a))


@step()
def s_rot_check():
    import math as _m
    op = live()
    deg = _m.degrees(op.angle)
    check(abs(deg - 90) < 3, f"マウスを中心のまわりに左へ 90° 回すと 90°（{deg:.1f}°）")
    S["deg"] = deg
    tap('R')


@step()
def s_rot_view():
    import math as _m
    op = live()
    check(op.rot_view is True and abs(_m.degrees(op.angle) - S["deg"]) < 1e-6,
          f"もう一度 R で軸が画面の奥向きに・角度はそのまま（{op.rot_view}・{_m.degrees(op.angle):.1f}°）")
    shot("11_rotate.png")
    for ch, t in [("1", 'ONE'), ("5", 'FIVE')]:
        tap(t, unicode=ch)


@step()
def s_rot_numeric():
    import math as _m
    op = live()
    check(abs(_m.degrees(op.angle) - 15) < 1e-6, f"数値入力 15 で 15°（{_m.degrees(op.angle):.3f}°）")
    tap('RET')


@step()
def s_rot_confirm():
    import math as _m
    check(live() is None, "回転を Enter で確定")
    last = bpy.context.window_manager.operators[-1]
    check(last.properties.mode == 'ROTATE' and abs(_m.degrees(last.properties.angle) - 15) < 1e-6,
          f"F9 に残るのは 回転・15°（{last.properties.mode}・{_m.degrees(last.properties.angle):.3f}°）")
    move(*S["c"])
    tap('ESC')


@step()
def s_menu0():
    check(live() is None, "回転のあとも、ふつうに戻っている")
    area, region = area_region()
    move(*S["c"])
    with bpy.context.temp_override(window=win(), area=area, region=region):
        bpy.ops.wm.call_menu(name="VIEW3D_MT_edit_mesh_context_menu")


@step(0.6)
def s_menu1():
    shot("06_context_menu.png")
    tap('UP_ARROW')
    tap('UP_ARROW')


@step()
def s_menu2():
    tap('RET')


@step(0.6)
def s_menu3():
    check(live() is None, "メニューで選んだだけでは始まらない（十字カーソルで待つ）")
    cx, cy = S["c"]
    move(cx + 40, cy)
    send('LEFTMOUSE')
    send('LEFTMOUSE', 'RELEASE')


@step()
def s_menu4():
    op = live()
    check(op is not None, "十字カーソルでクリックすると始まる")
    if op is not None:
        area, region = area_region()
        cx, cy = S["c"]
        exp = Vector((cx + 40 - region.x, cy - region.y))
        check((op.start_mouse - exp).length <= 1.0, f"始まりの位置はクリックした所（{tuple(op.start_mouse)} / {tuple(exp)}）")
        tap('ESC')


@step()
def s_panel():
    check(live() is None, "Esc でキャンセル")
    area, region = area_region()
    area.spaces.active.show_region_ui = True
    area.tag_redraw()


@step(0.6)
def s_panel_tab():
    area, region = area_region()
    ui = next(r for r in area.regions if r.type == 'UI')
    try:
        ui.active_panel_category = "面スライド"
    except Exception:  # noqa: BLE001
        log("panel category set failed")
    area.tag_redraw()


@step(0.8)
def s_panel_shot():
    shot("07_panel.png")
    # N パネルの「スライドを始める」：押したあと 3D ビューでクリックして始まるか
    area, region = area_region()
    ui = next(r for r in area.regions if r.type == 'UI')
    # ボタンはパネルの見出しのすぐ下（画面の撮影で x≈1160・上から 127px、1600×950 の窓）
    S["btn"] = (ui.x + ui.width // 2, ui.y + ui.height - int(45 * bpy.context.preferences.view.ui_scale))
    move(*S["btn"])
    send('LEFTMOUSE')
    send('LEFTMOUSE', 'RELEASE')


@step(0.6)
def s_panel_pending():
    check(live() is None, "N パネルのボタンを押しただけでは始まらない（十字カーソルで待つ）")
    cx, cy = S["c"]
    move(cx + 20, cy)
    send('LEFTMOUSE')
    send('LEFTMOUSE', 'RELEASE')


@step()
def s_panel_started():
    op = live()
    check(op is not None, "N パネルのボタン → 3D ビューでクリックで始まった")
    if op is not None:
        area, region = area_region()
        check(op.region.as_pointer() == region.as_pointer(), "動かすのは 3D ビューの本体（N パネルではない）")
        tap('ESC')


@step()
def s_grid():
    check(live() is None, "Esc でやめた")
    bpy.ops.object.mode_set(mode='OBJECT')
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o)
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=20, y_subdivisions=20, size=0.1, rotation=(1.5707963, 0, 0))
    obj = bpy.context.object
    bpy.ops.object.transform_apply(rotation=True)
    obj.shape_key_add(name="Basis")
    obj.shape_key_add(name="aa")
    obj.active_shape_key_index = 1
    bpy.ops.object.mode_set(mode='EDIT')
    bm = bmesh.from_edit_mesh(obj.data)
    for f in bm.faces:
        if f.normal.y > 0:
            f.normal_flip()
    mid = [e for e in bm.edges if all(abs(v.co.z) < 1e-6 for v in e.verts) and all(abs(v.co.x) < 0.0325 for v in e.verts)]
    bmesh.ops.split_edges(bm, edges=mid)
    for f in bm.faces:
        f.select = False
    for v in bm.verts:
        v.select = abs(v.co.z) < 1e-6 and abs(v.co.x) < 0.0325
    bm.select_flush_mode()
    bmesh.update_edit_mesh(obj.data)
    front_view()
    bpy.context.scene.tool_settings.proportional_size = 0.03
    bpy.context.scene.surface_slide.last_radius = 0.03


@step(0.6)
def s_open_start():
    area, region = area_region()
    S["c"] = (region.x + region.width // 2, region.y + region.height // 2)
    move(*S["c"])
    shortcut()


@step()
def s_open_a():
    obj = bpy.context.edit_object
    bm = bmesh.from_edit_mesh(obj.data)
    log("grid: sel", sum(v.select for v in bm.verts), "key", obj.active_shape_key and obj.active_shape_key.name,
        "poll", bpy.ops.mesh.surface_slide_proportional.poll(), "mode", bpy.context.mode)
    if live() is None:
        shot("07b_grid_not_started.png")
    check(live() is not None, "格子でも始まった")
    tap('A')


@step()
def s_open_drag():
    op = live()
    check(op is not None and op.mode == 'OPEN', f"A で口を開くになった（{op and op.mode}）")
    if op and op.open_info:
        check(op.open_info['upper'] > 0 and op.open_info['lower'] > 0, f"上唇・下唇に分かれた（{op.open_info}）")
    cx, cy = S["c"]
    for k in range(1, 8):
        move(cx + 3 * k, cy - k * 12)


@step()
def s_open_shot():
    op = live()
    check(op.offset[2] < 0 and abs(op.offset[0]) < 1e-9, f"口を開く：上下だけに動く（{tuple(round(x, 4) for x in op.offset)}）")
    shot("08_open.png")
    send('LEFTMOUSE')
    send('LEFTMOUSE', 'RELEASE')


@step()
def s_redo_panel():
    check(live() is None, "口を開くを確定")
    shot("09_open_confirmed.png")
    move(*S["c"])
    tap('F9')


@step(0.8)
def s_redo_shot():
    shot("10_redo_f9.png")
    tap('ESC')


@step()
def s_new_key_start():
    area, region = area_region()
    move(*S["c"])
    with bpy.context.temp_override(window=win(), area=area, region=region):
        r = bpy.ops.mesh.surface_slide_new_key('INVOKE_DEFAULT', name="smile")
    log("new key invoke", r)


@step()
def s_new_key():
    obj = bpy.context.edit_object
    check(obj.active_shape_key.name == "smile", f"新しいキー smile が書き込み先（{obj.active_shape_key.name}）")
    check(live() is not None, "新しいキーを作ってそのまま始まった")
    if live():
        tap('ESC')


@step()
def s_new_key_cancel():
    obj = bpy.context.edit_object
    names = [k.name for k in obj.data.shape_keys.key_blocks]
    check("smile" not in names and names == ["Basis", "aa"], f"やめたら作ったキーは消える（{names}）")


@step()
def s_error0():
    area, region = area_region()
    S["c"] = (region.x + region.width // 2, region.y + region.height // 2)
    move(*S["c"])
    shortcut()


@step()
def s_error_prep():
    op = live()
    check(op is not None, "エラーの確認のために始めた")
    S["before_err"] = [c.copy() for c in op.ctx.start]
    cx, cy = S["c"]
    move(cx, cy + 40)


@step()
def s_error1():
    op = live()

    def boom(*a, **k):
        raise ValueError("test error (ui_smoke)")
    op.ctx.solve = boom
    cx, cy = S["c"]
    move(cx, cy + 50)


@step()
def s_error2():
    check(live() is None, "エラーで止まった")
    obj = bpy.context.edit_object
    bm = bmesh.from_edit_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    d = max((v.co - c).length for v, c in zip(bm.verts, S["before_err"]))
    check(d < 1e-9, f"エラーのとき形は始めに戻った（差 {d:.1e}）")
    from bl_ui.space_statusbar import STATUSBAR_HT_header
    check(STATUSBAR_HT_header.draw is getattr(STATUSBAR_HT_header, "_draw_orig", None), "エラーのあともステータスバーが戻った")
@step()
def s_end():
    log("RESULT", "PASS" if S["fail"] == 0 else f"FAIL {S['fail']}")
    bpy.ops.wm.quit_blender()


def tick():
    if not STEPS:
        return None
    fn, delay = STEPS.pop(0)
    try:
        fn()
    except Exception:  # noqa: BLE001
        log("ERROR in", fn.__name__, traceback.format_exc()[-1500:])
        S["fail"] += 1
        log("RESULT", f"FAIL {S['fail']}")
        bpy.ops.wm.quit_blender()
        return None
    return delay


bpy.app.timers.register(tick, first_interval=2.0)
