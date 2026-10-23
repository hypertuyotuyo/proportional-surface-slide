"""画面に出す文言（ヘッダー・ステータスバー・メッセージ）。bpy の UI に触らない純関数にしてテストする。

ステータスバーの並びとマニュアルのキー表は、ここの STATUS_KEYS から作る（食い違わないように）。
"""
import math

import bpy

from . import core

TOOL_NAME = "面に沿ってスライド"
MENU_TEXT = "面に沿ってスライド（プロポーショナル）"
NEW_KEY_TEXT = "新しいキーを作って面に沿ってスライド"
PANEL_TAB = "面スライド"

MODE_NAMES = {'MOVE': "移動", 'SCALE': "拡大縮小", 'ROTATE': "回転", 'OPEN': "口を開く"}
REF_NAMES = {'START': "いまの形", 'BASIS': "ベースの形"}
SPACE_NAMES = {'GLOBAL': "グローバル", 'LOCAL': "ローカル"}

MSG = {
    'no_selection': "動かしたい頂点を選んでから使ってください（面に沿ってスライド）",
    'basis_active': ("書き込み先がベース（Basis）なので止めました。このまま動かすと、ほかの表情キーも全部変わります。"
                     "シェイプキーの一覧で書き込み先のキーを選んでください"),
    'locked': "シェイプキー「{key}」はロックされています。一覧の鍵を外してからやり直してください",
    'multi_object': "動くのはアクティブな「{name}」だけです（ほかのオブジェクトの選択は使いません）",
    'radius_big': "範囲の半径 {old} は大きすぎるので {new} から始めます（ホイールで変えられます）",
    'radius_small': "範囲の半径 {old} は小さすぎるので {new} から始めます（ホイールで変えられます）",
    'random_falloff': "減衰「ランダム」は使えないので「スムーズ」で動かします",
    'open_failed': "上下に分けられませんでした。唇の切れ目のまわり（上唇と下唇の縁）の頂点を選んでください",
    'error': "面に沿ってスライドでエラーが起きたので元に戻しました（詳しくはシステムコンソール）",
    'not_view3d': "3D ビューの上で使ってください",
    'key_dropped': "やめたので、作ったキーは消しました",
}


def fmt_len(scene, v):
    """シーンの単位で長さを書く（'6.3 mm'・'1.23 cm'）。"""
    us = scene.unit_settings if scene is not None else None
    if us is None or us.system == 'NONE':
        return f"{v:.4g}"
    try:
        return bpy.utils.units.to_string(us.system, 'LENGTH', v * us.scale_length, precision=3).strip()
    except Exception:  # noqa: BLE001
        return f"{v:.4g}"


def axis_label(state):
    if state.axis == 'NONE':
        return ""
    space = SPACE_NAMES.get(state.axis_space, "")
    if state.axis_plane:
        return f"（{state.axis} を除く・{space}）"
    return f"（{state.axis}・{space}）"


def rot_axis_label(state):
    if state.axis != 'NONE':
        return f"（{state.axis} のまわり・{SPACE_NAMES.get(state.axis_space, '')}）"
    return "（画面の奥向きのまわり）" if getattr(state, 'rot_view', False) else "（面の向きのまわり）"


def target_label(obj):
    keys = obj.data.shape_keys if obj is not None else None
    if keys is None or obj.active_shape_key is None:
        return "メッシュ（シェイプキーなし）"
    return obj.active_shape_key.name


def header_text(state, scene, obj):
    """3D ビューのヘッダーに出す1行。state はオペレーター（か、同じ名前の属性を持つもの）。"""
    parts = []
    mode = state.mode
    if state.num_text:
        parts.append(f"{MODE_NAMES[mode]} {state.num_text}▏（数値入力：Enter で確定・Backspace で消す）"
                     + (" ⚠ 読めません" if state.num_error else ""))
    elif mode == 'SCALE':
        parts.append(f"拡大縮小 ×{state.scale:.3f}{axis_label(state)}")
    elif mode == 'ROTATE':
        parts.append(f"回転 {math.degrees(getattr(state, 'angle', 0.0)):.1f}°{rot_axis_label(state)}")
    else:
        dist = state.offset_length
        parts.append(f"{MODE_NAMES[mode]} {fmt_len(scene, dist)}{axis_label(state)}")
    dist_mode = "つながった面だけ" if state.connected else "まっすぐの距離"
    parts.append(f"範囲 半径 {fmt_len(scene, state.radius)}・{core.FALLOFF_NAMES.get(state.falloff, state.falloff)}・{dist_mode}")
    parts.append(f"書き込み先：{target_label(obj)}")
    parts.append(f"影響 {state.affected} / {state.visible} 頂点")
    if state.use_mirror:
        parts.append("左右対称")
    if mode == 'OPEN' and state.open_info:
        oi = state.open_info
        parts.append(f"上唇 {oi['upper']}・下唇 {oi['lower']}・口角 {oi['corner']}（上唇は {state.upper_ratio:.2f} 倍）")
    if not state.snap:
        parts.append("⚠ 面に沿わせていません")
    if state.reference == 'BASIS':
        parts.append("沿わせる面：ベースの形")
    if state.use_mirror and state.mirror_missing:
        parts.append(f"⚠ 左右の相手が見つからない頂点：{state.mirror_missing} 個（片側だけ動きます）")
    if state.hint:
        parts.append(state.hint)
    return "　｜　".join(parts)


# ステータスバー：(アイコンの並び, 文言, 状態を出すか)
def status_items(state, has_keys=False):
    """[(icons, text, active, gap_after)]。active=False はグレー表示。"""
    on = lambda b: "ON" if b else "OFF"  # noqa: E731
    axis_txt = "なし" if state.axis == 'NONE' else (
        f"{state.axis}{' を除く' if state.axis_plane else ''} {SPACE_NAMES.get(state.axis_space, '')}")
    if state.mode == 'ROTATE' and state.axis == 'NONE':
        rot_item = (('EVENT_R',), f"回転の軸：{'画面の奥' if getattr(state, 'rot_view', False) else '面の向き'}", True, False)
    else:
        rot_item = (('EVENT_R',), "回転", state.mode != 'ROTATE', False)
    items = [
        (('MOUSE_LMB',), "確定", True, False),
        (('MOUSE_RMB',), "キャンセル", True, True),
        (('MOUSE_MMB_SCROLL',), "範囲", True, False),
        (('EVENT_SHIFT',), "ゆっくり", True, True),
        (('EVENT_G',), "移動", state.mode != 'MOVE', False),
        (('EVENT_S',), "拡大縮小", state.mode != 'SCALE', False),
        rot_item,
        (('EVENT_A',), "口を開く", state.mode != 'OPEN', True),
        (('EVENT_X', 'EVENT_Y', 'EVENT_Z'), f"軸：{axis_txt}", True, state.axis == 'NONE'),
    ]
    if state.axis != 'NONE':
        items.append((('EVENT_C',), "軸を解除", True, True))
    items += [
        (('EVENT_SHIFT', 'EVENT_O'), f"減衰：{core.FALLOFF_NAMES.get(state.falloff, state.falloff)}", True, False),
        (('EVENT_ALT', 'EVENT_O'), f"つながった面だけ：{on(state.connected)}", True, False),
        (('EVENT_F',), f"面に沿わせる：{on(state.snap)}", True, False),
    ]
    if has_keys:
        items.append((('EVENT_B',), f"沿わせる面：{REF_NAMES[state.reference]}", True, False))
    items.append((('EVENT_M',), f"左右対称：{on(state.use_mirror)}", True, True))
    items.append(((), "0-9 数値", True, False))
    return items


# マニュアルの「操作中のキー」の表（ステータスバーと同じ並び）
MODAL_KEYS = [
    ("左クリック・Enter・スペース", "確定", "Blender の G と同じ"),
    ("右クリック・Esc", "キャンセル（元に戻す）", "Blender の G と同じ"),
    ("ホイール下・PageUp", "範囲を広く", "Blender の G と同じ"),
    ("ホイール上・PageDown", "範囲を狭く", "Blender の G と同じ"),
    ("Shift（押している間）", "ゆっくり動く・範囲をゆっくり変える", "Blender の G と同じ"),
    ("G", "移動に切り替え", "Blender の G と同じ"),
    ("S", "拡大縮小に切り替え（選択の中心から）", "Blender の S と同じ"),
    ("R", "回転に切り替え（回転中にもう一度 R で、軸を 面の向き ⇄ 画面の奥向き）", "Blender の R に近い"),
    ("A", "口を開くに切り替え（下唇は下へ、上唇は少し上へ）", "この道具だけ"),
    ("X / Y / Z", "その軸だけに動かす・回転ではその軸のまわりに回す（押すたびに グローバル → ローカル → 解除）", "Blender の G と同じ"),
    ("Shift + X / Y / Z", "その軸を除いた 2 方向に動かす", "Blender の G と同じ"),
    ("C", "軸を解除", "Blender の G と同じ"),
    ("Shift + O（O でも）", "減衰の曲線を順に切り替え", "Blender の G と同じ"),
    ("Alt + O", "つながった面だけ ON/OFF", "Blender の「接続のみ」と同じ"),
    ("F", "面に沿わせる ON/OFF", "この道具だけ"),
    ("B", "沿わせる面：いまの形 ⇄ ベースの形", "この道具だけ"),
    ("M", "左右対称 ON/OFF", "この道具だけ"),
    ("0〜9 . -", "数値で入力（回転は度。= で単位や式も）", "Blender の G と同じ"),
    ("Alt + 中ボタン・Alt + ホイール", "操作を続けたまま視点を動かす", "Blender の G と同じ"),
]
